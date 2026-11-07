"""Flask HTTP UI for the persistent, single-GPU transcription queue."""
import os
import shutil
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge

from jobs import Dispatcher, JobStore, TERMINAL
from worker import run_job
from asr import DEFAULT_PROFILE, PROFILES

BASE = Path(__file__).resolve().parent
DATA = Path(os.getenv('DATA_DIR', str(BASE / 'data'))).resolve()
STORE = JobStore(DATA)
DISPATCHER = Dispatcher(STORE, run_job)
HISTORY_LOCK = threading.Lock()
ALLOWED = {'.mp3', '.m4a', '.wav', '.flac', '.mp4', '.aac', '.ogg', '.webm', '.mov'}
FILES = {'txt': 'result.txt', 'srt': 'result.srt', 'captions': 'captions.txt',
         'raw': 'raw.txt', 'stats': 'stats.json', 'asr': 'asr_chunks.json'}
MAX_BYTES = 500 * 1024 * 1024
HOST = os.getenv('HOST', '0.0.0.0')

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = MAX_BYTES


@app.get('/')
def home():
    return render_template('index.html', model_profiles=PROFILES, default_profile=DEFAULT_PROFILE)


@app.get('/api/jobs')
def list_jobs():
    return jsonify(jobs=STORE.list(limit=200), **STORE.summary())


def remove_job_files(job_id):
    folder = (DATA / job_id).resolve()
    if folder.parent != DATA or folder.name != job_id:
        raise ValueError('任务目录超出数据目录')
    shutil.rmtree(folder, ignore_errors=True)


@app.delete('/api/jobs')
def clear_jobs():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error='请指定要删除的任务或清空历史记录'), 400
    if payload.get('all') is True and 'ids' not in payload:
        ids = None
    elif 'all' not in payload and isinstance(payload.get('ids'), list):
        ids = payload['ids']
        if len(ids) > 200 or any(not isinstance(value, str) or not value for value in ids):
            return jsonify(error='每次最多选择 200 条有效任务记录'), 400
        ids = list(dict.fromkeys(ids))
    else:
        return jsonify(error='请指定任务列表 ids，或使用 all 清空历史记录'), 400
    with HISTORY_LOCK:
        deleted = STORE.delete_many(ids)
        for job_id in deleted:
            remove_job_files(job_id)
    return jsonify(ok=True, deleted_ids=deleted, deleted_count=len(deleted))


@app.post('/api/jobs')
def create_job():
    model_profile = request.form.get('model_profile', DEFAULT_PROFILE)
    if model_profile not in PROFILES:
        return jsonify(error='请选择日常或高准确率识别模式'), 400
    uploaded = request.files.get('file')
    if not uploaded or not uploaded.filename:
        return jsonify(error='请先选择音频或视频文件'), 400
    name = Path(uploaded.filename).name[:160]
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED:
        return jsonify(error='不支持该格式'), 400
    job_id = uuid.uuid4().hex
    folder = DATA / job_id
    folder.mkdir(parents=True)
    source = folder / ('upload' + suffix)
    try:
        uploaded.save(source)
        STORE.add(job_id, name, suffix, source.stat().st_size, model_profile=model_profile)
    except Exception:
        remove_job_files(job_id)
        raise
    return jsonify(id=job_id, status='queued'), 201


@app.post('/api/jobs/<job_id>/retry')
def retry_job(job_id):
    # Keep the original upload alive while another request clears history.
    with HISTORY_LOCK:
        return create_retry(job_id)


def create_retry(job_id):
    previous = STORE.get(job_id)
    if previous is None:
        return jsonify(error='任务不存在'), 404
    if previous['status'] != 'error':
        return jsonify(error='只能重试失败的任务'), 409
    payload = request.get_json(silent=True)
    if payload is None and not request.get_data():
        payload = {}
    if not isinstance(payload, dict):
        return jsonify(error='重试参数无效'), 400
    profile = payload.get('model_profile', previous.get('model_profile', DEFAULT_PROFILE))
    if not isinstance(profile, str) or profile not in PROFILES:
        return jsonify(error='请选择日常或高准确率识别模式'), 400
    source = DATA / job_id / ('upload' + previous['extension'])
    if not source.is_file():
        return jsonify(error='原始文件已不存在，请重新上传音视频文件'), 409
    retry_id = uuid.uuid4().hex
    folder = DATA / retry_id
    folder.mkdir(parents=True)
    copied = folder / source.name
    try:
        shutil.copyfile(source, copied)
        model = previous.get('model') if (
            previous.get('engine') == 'qwen3-asr' and profile == previous.get('model_profile')
        ) else None
        STORE.add(retry_id, previous['filename'], previous['extension'], copied.stat().st_size,
                  model_profile=profile, retry_of=job_id, model=model)
    except FileNotFoundError:
        remove_job_files(retry_id)
        return jsonify(error='原始文件已被删除，请重新上传音视频文件'), 409
    except Exception:
        remove_job_files(retry_id)
        raise
    return jsonify(id=retry_id, status='queued', retry_of=job_id), 201


@app.get('/api/jobs/latest-failed')
def latest_failed_job():
    job = STORE.latest_failed()
    if job is None:
        return jsonify(error='暂无失败记录'), 404
    return job_details(job)


@app.get('/api/jobs/<job_id>')
def get_status(job_id):
    job = STORE.get(job_id)
    if job is None:
        return jsonify(error='任务不存在'), 404
    return job_details(job)


def job_details(job):
    folder = DATA / job['id']
    job['can_retry'] = job['status'] == 'error' and (folder / ('upload' + job['extension'])).is_file()
    # Only one opened job needs transcript contents; list endpoint is lightweight.
    for key, filename in [('article', 'result.txt'), ('captions', 'captions.txt'), ('raw', 'raw.txt')]:
        path = folder / filename
        try:
            job[key] = path.read_text(encoding='utf-8') if path.is_file() else ''
        except OSError:
            job[key] = ''
    job['available_downloads'] = [
        kind for kind, filename in FILES.items() if (folder / filename).is_file()
    ]
    return jsonify(job)


@app.post('/api/jobs/<job_id>/cancel')
def cancel_job(job_id):
    result = STORE.cancel(job_id)
    if result == 'missing':
        return jsonify(error='任务不存在'), 404
    if result == 'terminal':
        return jsonify(error='任务已经结束，不能取消'), 409
    return jsonify(status=result, message='取消请求已提交')


@app.delete('/api/jobs/<job_id>')
def delete_job(job_id):
    with HISTORY_LOCK:
        result = STORE.delete(job_id)
        if result == 'deleted':
            remove_job_files(job_id)
    if result == 'missing':
        return jsonify(error='任务不存在'), 404
    if result == 'active':
        return jsonify(error='只能删除已完成、失败或取消的任务'), 409
    return jsonify(ok=True)


@app.get('/api/jobs/<job_id>/download/<kind>')
def download(job_id, kind):
    job = STORE.get(job_id)
    if job is None:
        return jsonify(error='任务不存在'), 404
    if job['status'] not in TERMINAL:
        return jsonify(error='任务仍在运行，请完成或取消后下载'), 409
    if kind not in FILES:
        return jsonify(error='不支持的下载类型'), 400
    filename = DATA / job_id / FILES[kind]
    if not filename.is_file():
        return jsonify(error='结果文件不存在'), 404
    media_type = 'application/json' if kind in {'stats', 'asr'} else 'text/plain; charset=utf-8'
    return send_file(filename, as_attachment=True, download_name=filename.name, mimetype=media_type)


@app.errorhandler(RequestEntityTooLarge)
def too_big(error):
    return jsonify(error='文件超过 500MB 限制'), 413


def main():
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise SystemExit('请先安装 FFmpeg：brew install ffmpeg')
    port = int(os.getenv('PORT', '5840'))
    DISPATCHER.start()
    print(f'本机访问：http://127.0.0.1:{port}')
    print(f'监听地址：{HOST}:{port}（无登录验证）')
    app.run(host=HOST, port=port, debug=False,
            threaded=True, use_reloader=False)


if __name__ == '__main__':
    main()
