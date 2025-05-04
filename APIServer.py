from flask import Flask, request, jsonify
import os
import subprocess
import glob
import pyminizip, secrets, string
import re, fcntl, errno, time,tempfile, pathlib, urllib.parse, requests, shutil
from werkzeug.utils import secure_filename
import threading, time, hashlib

DYNAMIC_THRESHOLD = 41
start_ts = None                 # время старта записи
det_times = {}
AV_NAMES = [
    "Avast",
    "Avira"
]


_current = {"total": 0, "det": {}, "start": None}                 # {session: {"total":int, "det":set(), "start":t}}
_ses_lock   = threading.Lock()

app = Flask(__name__)

# ──── ADD: энд‑поинт «/start» (вызывает vnc_back.sh) ────
@app.route("/start/<int:total>", methods=["POST"])
def _detect_start(total):
    with _ses_lock:
        _current["total"] = total
        _current["det"].clear()
        _current["start"] = time.time()
    return ("", 204)
# ─────────────────────────────────────────────────────────

# ──── ADD: энд‑поинт «/detect» (шлёт AHK в госте) ───────
@app.route("/detect/<vmid>", methods=["POST"])
def _detect_mark(vmid):
    with _ses_lock:
        _current["det"][str(vmid)] = time.time()
    return ("", 204)
# ─────────────────────────────────────────────────────────

def build_status_table(vm_ids) -> dict[str, str]:
    """
    Возвращает {vmid: status}.
      clean   – совсем не было /detect
      static  – /detect пришёл ≤ 40 c после /start
      dynamic – /detect пришёл > 40 c после /start
    """
    with _ses_lock:
        t0   = _current["start"]
        det  = _current["det"].copy()

    statuses = {}
    for vmid in vm_ids:
        vid = str(vmid)
        if vid not in det or t0 is None:
            statuses[vid] = "Clean"
        else:
            delta = det[vid] - t0
            statuses[vid] = "Static" if delta <= 40 else "Runtime"
    return statuses

from jinja2 import Environment, FileSystemLoader, select_autoescape
# …

def build_av_statuses(statuses: dict[str, str],
                      mp4_files: list[str]) -> dict[str, dict]:
    """
    >>> statuses   = {"ClamAV": "OK", "DrWeb": "Found"}
    >>> mp4_files  = ["/root/DrWeb_42.mp4", "/root/other.mp4"]
    >>> build_av_statuses(statuses, mp4_files)
    {'ClamAV': {'status': 'OK', 'video': False},
     'DrWeb':  {'status': 'Found', 'video': True}}
    """
    av_statuses = {}
    for av in AV_NAMES:
        video = next(
            (os.path.basename(p) for p in mp4_files
             if av.lower() in os.path.basename(p).lower()),
            ""
        )
        av_statuses[av] = {
            "status":  statuses.get(av, "Clean"),
            "video":  video,            # '' или имя ролика
        }
    return av_statuses

# ─── инициализируем Jinja один раз, где‑нибудь после конфига ─
env = Environment(
    loader=FileSystemLoader("."),          # ищем шаблоны в корне проекта
    autoescape=select_autoescape(["html"])
)
tmpl_scan = env.get_template("base.html")

def make_html_report(file_path: pathlib.Path,
                     av_statuses: dict[str, str],
                     vm_totals: dict[str, int],
                     mp4_files: list[str]) -> pathlib.Path:
    """
    • file_path      – загруженный пользователем файл
    • av_statuses    – {av_name: {"status": str, "video": bool}}
    • vm_totals      – {"clean": n1, "static": n2, "runtime": n3}
    """
    
    stat = file_path.stat()
    with open(file_path, "rb") as f:
        data = f.read()

    ctx = {
        # FILE INFO
        "file_name":  file_path.name,
        "size":       stat.st_size,
        "scan_date":  time.strftime("%Y‑%m‑%d %H:%M:%S"),
        "md5":        hashlib.md5(data).hexdigest(),
        "sha256":     hashlib.sha256(data).hexdigest(),
        "file_type":  subprocess.check_output(
                          ["file", "-b", str(file_path)]
                      ).decode().strip(),
        "magic":      subprocess.check_output(
                          ["file", "-b", "--mime", str(file_path)]
                      ).decode().strip(),
        "signature":  "",                     # если нужна своя логика
        # АВ результаты
        "av_results": [
            {"name": k,
             "status": v["status"],
             "video":  v["video"]}
            for k, v in av_statuses.items()
        ],
        # totals
        "totals": vm_totals,
    }

    html_text = tmpl_scan.render(**ctx)

    tmp_html = pathlib.Path(tempfile.mkstemp(prefix="report_", suffix=".html")[1])
    tmp_html.write_text(html_text, encoding="utf‑8")
    return tmp_html

LOCK_PATH = "/var/run/apisrv.lock"
COOLDOWN  = 120
VM_IDS  = [101, 102]         # какие ВМ должны быть «готовы»
MIN_UP  = 180                # минимум 3 минуты

re_status  = re.compile(r'^status:\s+(\w+)', re.M)
re_uptime  = re.compile(r'^uptime:\s+(\d+)', re.M)

def save_remote(url: str) -> pathlib.Path:
    """Скачивает файл по ссылке и возвращает путь к временному файлу."""
    local = TMPDIR / (secrets.token_hex(6) + "_" + os.path.basename(url))
    with requests.get(url, stream=True, timeout=30) as r:
        r.raise_for_status()
        with open(local, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
    return local

def get_qm_state(vmid: int):
    """Вернёт (status:str, uptime:int) по выводу `qm status`."""
    out = subprocess.check_output(
        ["/usr/sbin/qm", "status", str(vmid), "--verbose"],
        text=True
    )
    status = re_status.search(out).group(1)
    uptime = int(re_uptime.search(out).group(1)) if re_uptime.search(out) else 0
    return status, uptime

def acquire_lock():
    """Вернёт True, если lock получен; иначе False + msg"""
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_WRONLY | os.O_EXCL)
        os.write(fd, str(time.time()).encode())
        os.close(fd)
        return True, None
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise
        # lock‑файл уже есть → проверяем возраст
        age = time.time() - os.path.getmtime(LOCK_PATH)
        if age < COOLDOWN:
            return False, f"Server busy, wait {int(COOLDOWN - age)}s"
        # lock старый → пытаемся «перехватить»
        try:
            os.remove(LOCK_PATH)
            return acquire_lock()
        except OSError:
            return False, "Could not acquire lock"

def release_lock():
    try:
        os.remove(LOCK_PATH)
    except FileNotFoundError:
        pass

@app.route('/upload', methods=['POST'])
def upload_and_process():
    ok, msg = acquire_lock()
    if not ok:
        return jsonify(error=msg), 429
    try:
    	# 1. Проверяем наличие файла и параметра в запросе
    	try:
    	    param = int(request.form.get('time', '0'))
    	except ValueError:
    	    return jsonify(error="'time' must be integer"), 400
    	if param > 300:
    	    return jsonify(error="'time' can not be more than 300 seconds"), 400

    	for vmid in VM_IDS:
        	try:
        	    status, uptime = get_qm_state(vmid)
        	except (subprocess.CalledProcessError, AttributeError):
        	    return jsonify(error=f"VM {vmid}: cannot read status"), 400

        	if status != "running":
        	    return jsonify(error=f"Starting VM, wait"), 400
        	if uptime < MIN_UP:
        	    wait = MIN_UP - uptime
        	    return jsonify(error=f"VM up {uptime}s, wait {wait}s"), 400

    	#has_file = "file" in request.files and request.files["file"].filename
    	#has_link = "link" in request.form and request.form["link"].strip()
    	file_obj   = request.files.get("file")          # None, если поля нет
    	link_value = request.form.get("link", "").strip()

    	has_file = bool(file_obj and file_obj.filename) # True, если файл реально есть
    	has_link = bool(link_value)                     # True, если непустая ссылка

	# --- допускаем *ровно одно* из двух ------------------------------------------
    	if not (has_file ^ has_link):                   # XOR: True, если одно И только одно
    		return jsonify(error="provide either file OR link"), 400
    	if has_file:
    		upfile   = request.files["file"]
    		filename = secure_filename(upfile.filename)
    	else:                                         # пришла ссылка
    		url      = request.form["link"].strip()
    		filename = secure_filename(
    		pathlib.Path(urllib.parse.urlparse(url).path).name or "download.bin")
    	# Сохраняем файл во временное место
    	saved_path = os.path.join('/tmp', filename)
    	
    	try:
    		if has_file:
    			upfile.save(saved_path)                          # локальный upload
    		else:
    			with requests.get(url, stream=True, timeout=30, headers={'User-Agent':'Mozilla/5.0'}) as r:
    				r.raise_for_status()
    				with open(saved_path, "wb") as dst:
    					shutil.copyfileobj(r.raw, dst)           # скачать по ссылке
    	except Exception as e:
    		return jsonify(error=f"Error saving file: {e}"), 500

    	# 2. Отправляем файл на SMB-шару //192.168.100.10/Share
    	try:
        	subprocess.run([
        	    "smbclient", "//192.168.100.10/Share",
        	    "-N", "-c", f"put {saved_path} {filename}"
        	], check=True)
    	except subprocess.CalledProcessError as e:
                os.remove(saved_path)
                return jsonify(error=f"Error with SMB: {e}"), 500

    	# 3. Удаляем старые .mp4 из /root перед запуском скрипта
    	for old_mp4 in glob.glob('/root/*.mp4'):  # ищем файлы .mp4&#8203;:contentReference[oaicite:7]{index=7}
                try:
                    os.remove(old_mp4)
                except OSError as e:
                    # Если не удалось удалить старый файл – игнорируем или логируем
                    pass
    	for old_flv in glob.glob('/root/*.flv'):  # ищем файлы .mp4&#8203;:contentReference[oaicite:7]{index=7}
       		try:
        	    os.remove(old_flv)
        	except OSError as e:
        	    # Если не удалось удалить старый файл – игнорируем или логируем
        	    pass

    	# 4. Запускаем скрипт vnc_back.sh с параметром и ждём завершения&#8203;:contentReference[oaicite:8]{index=8}
    	print("== START vnc_back ==")
    	try:
    	    result = subprocess.run(['bash', 'vnc_back.sh', str(param)], check=True, capture_output=True, text=True)
    	except subprocess.CalledProcessError as e:
            subprocess.run(["smbclient", "//192.168.100.10/Share","-N", "-c", f"del {filename}"], check=True)
            os.remove(saved_path)
            return jsonify(error=f"vnc_back.sh end with error: {e.stderr}"), 500
    	print("== DONE vnc_back ==")
    	# 5. После завершения скрипта собираем новые .mp4

    	subprocess.run(["smbclient", "//192.168.100.10/Share","-N", "-c", f"del {filename}"], check=True)

    	print("== START mp4 ==")
    	for _ in range(30):
    		mp4_files = glob.glob('/root/*.mp4')
    		if mp4_files:
    			break
    		time.sleep(2)
    	else:
        	return jsonify(error="No MP4 after script"), 500
    	print("== DONE mp4 ==")
    	# 6. Упаковываем .mp4 в tar.gz архив
    	print("== START ZIP ==")
    	archive_path = '/tmp/report.zip'
    	alphabet = string.ascii_letters + string.digits
    	password = ''.join(secrets.choice(alphabet) for _ in range(8))
    	statuses = build_status_table(_current["det"])
    	av_statuses = build_av_statuses(statuses, mp4_files)
    	vm_totals = {
                "clean":   sum(1 for s in av_statuses.values() if s["status"] == "Clean"),
                "static":  sum(1 for s in statuses.values() if s == "Static"),
                "runtime": sum(1 for s in statuses.values() if s == "Runtime"),
    	}

    	# 5.2 создать .html
    	html_report = make_html_report(pathlib.Path(saved_path), av_statuses, vm_totals, mp4_files)

    	# 5.3 добавить html к списку файлов для архивации
    	files_to_zip = mp4_files + [str(html_report)]
    	arc_names    = [os.path.basename(p) for p in files_to_zip]
    	try:
             pyminizip.compress_multiple(
             files_to_zip, [],           # файлы и имена внутри zip
             archive_path,
             password,
             5                        # уровень сжатия
            )
    	except Exception as e:
            os.remove(archive_path)
            return jsonify(error=f"Create archive error: {e}"), 500
    	html_report.unlink(missing_ok=True)
    	print("== DONE ZIP ==")
    	# 7. Загружаем архив на temp.sh через curl
    	print("== START temp.sh ==")
    	try:
    	    upload_output = subprocess.check_output([
    	        "curl", "-s", "-F", f"file=@{archive_path}", "https://temp.sh/upload"
    	    ])
    	    link = upload_output.decode().strip()
    	except Exception as e:
    	    return jsonify(error=f"Error download on temp.sh: {e}"), 500
    	print("== DONE temp.sh ==")
    	detected = len(_current["det"])
    	total    = _current["total"]
    	detect_line = f"Detects {detected}/{total}"
    	os.remove(archive_path)
    	#os.remove(saved_path)
    	# 8. Возвращаем клиенту прямую ссылку в JSON
    	return jsonify(url=link, password=password, detects=detect_line)

    finally:
        release_lock()
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
