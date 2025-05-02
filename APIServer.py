from flask import Flask, request, jsonify
import os
import subprocess
import glob
import zipfile

app = Flask(__name__)

@app.route('/upload', methods=['POST'])
def upload_and_process():
    # 1. Проверяем наличие файла и параметра в запросе
    if 'file' not in request.files:
        return jsonify(error="Attach file"), 400

    file = request.files['file']
    param = request.form.get('param', '0')
    if file.filename == '':
        return jsonify(error="No file name"), 400

    # Сохраняем файл во временное место
    filename = os.path.basename(file.filename)  # можно использовать secure_filename для безопасности
    saved_path = os.path.join('/tmp', filename)
    try:
        file.save(saved_path)  # сохраняем .exe локально&#8203;:contentReference[oaicite:6]{index=6}
    except Exception as e:
        return jsonify(error=f"Error saving file: {e}"), 500

    # 2. Отправляем файл на SMB-шару //192.168.100.10/Share
    try:
        subprocess.run([
            "smbclient", "//192.168.100.10/Share",
            "-N", "-c", f"put {saved_path} {filename}"
        ], check=True)
    except subprocess.CalledProcessError as e:
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
        result = subprocess.run(['bash', 'vnc_back.sh', param], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
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
    try:
        with zipfile.ZipFile(archive_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for mp4 in mp4_files:
                zf.write(mp4, arcname=os.path.basename(mp4))
    except Exception as e:
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
    os.remove(saved_path)
    # 8. Возвращаем клиенту прямую ссылку в JSON
    return jsonify(url=link)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
