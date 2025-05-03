from flask import Flask, request, jsonify
import os
import subprocess
import glob
import pyminizip, secrets, string
import re, fcntl, errno, time,tempfile, pathlib, urllib.parse, requests, shutil
from werkzeug.utils import secure_filename

app = Flask(__name__)

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
        	    return jsonify(error=f"VM {vmid} is not running"), 400
        	if uptime < MIN_UP:
        	    wait = MIN_UP - uptime
        	    return jsonify(error=f"VM {vmid} up {uptime}s, wait {wait}s"), 400

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
    	try:
             pyminizip.compress_multiple(
             mp4_files, [],           # файлы и имена внутри zip
             archive_path,
             password,
             5                        # уровень сжатия
            )
    	except Exception as e:
            os.remove(archive_path)
            return jsonify(error=f"Create archive error: {e}"), 500
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
    	os.remove(archive_path)
    	#os.remove(saved_path)
    	# 8. Возвращаем клиенту прямую ссылку в JSON
    	return jsonify(url=link, password=password)

    finally:
        release_lock()
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
