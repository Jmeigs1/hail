"""
A Jupyter notebook service with local-mode Hail pre-installed
"""
import gevent
# must happen before anytyhing else
from gevent import monkey
monkey.patch_all()

import requests
from flask import Flask, session, redirect, render_template, request, jsonify
from flask_sockets import Sockets
import flask
import kubernetes as kube
import logging
import os
import re
import requests
import time
import uuid
import pymysql
from dotenv import load_dotenv

load_dotenv(verbose=True)

fmt = logging.Formatter(
    # NB: no space after levelname because WARNING is so long
    '%(levelname)s\t| %(asctime)s \t| %(filename)s \t| %(funcName)s:%(lineno)d | '
    '%(message)s')

fh = logging.FileHandler('notebook.log')
fh.setLevel(logging.INFO)
fh.setFormatter(fmt)

ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(fmt)

log = logging.getLogger('notebook')
log.setLevel(logging.INFO)
logging.basicConfig(
    handlers=[fh, ch],
    level=logging.INFO)

if 'BATCH_USE_KUBE_CONFIG' in os.environ:
    kube.config.load_kube_config()
else:
    kube.config.load_incluster_config()
k8s = kube.client.CoreV1Api()

app = Flask(__name__)
sockets = Sockets(app)


def read_string(f):
    with open(f, 'r') as f:
        return f.read().strip()


KUBERNETES_TIMEOUT_IN_SECONDS = float(
    os.environ.get('KUBERNETES_TIMEOUT_IN_SECONDS', 5.0))
app.secret_key = read_string('/notebook-secrets/secret-key')
PASSWORD = read_string('/notebook-secrets/password')
ADMIN_PASSWORD = read_string('/notebook-secrets/admin-password')
INSTANCE_ID = uuid.uuid4().hex

log.info(f'KUBERNETES_TIMEOUT_IN_SECONDS {KUBERNETES_TIMEOUT_IN_SECONDS}')
log.info(f'INSTANCE_ID {INSTANCE_ID}')

try:
    with open('notebook-worker-images', 'r') as f:
        def get_name(line):
            return re.search("/([^/:]+):", line).group(1)
        WORKER_IMAGES = {get_name(line): line.strip() for line in f}
except FileNotFoundError as e:
    raise ValueError(
        "working directory must contain a file called `notebook-worker-images' "
        "containing the name of the docker image to use for worker pods.") from e

CREATED_STATE = 'created'
DELETED_STATE = 'deleted'

AUTH_GATEWAY = os.environ.get("AUTH_GATEWAY")
DB = os.environ.get("MYSQL_DB")
IP = os.environ.get("MYSQL_IP")
PORT = int(os.environ.get("MYSQL_PORT"))
USER = os.environ.get("MYSQL_USER")
PASS = os.environ.get("MYSQL_PASS")
TABLE = 'sessions'
mysql_conn = pymysql.connect(host=IP, port=PORT, user=USER, passwd=PASS, db=DB)

cur = mysql_conn.cursor()

cur.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        user_id VARCHAR(255),
        pod_name VARCHAR(255),
        svc_name VARCHAR(255),
        token VARCHAR(255),
        state VARCHAR(255),
        name VARCHAR(255),
        PRIMARY KEY (user_id, token)
    ) ENGINE = INNODB;
""")

cur.close()
mysql_conn.commit()
mysql_conn.close()


def start_pod(jupyter_token, image):
    pod_id = uuid.uuid4().hex
    service_spec = kube.client.V1ServiceSpec(
        selector={
            'app': 'notebook-worker',
            'hail.is/notebook-instance': INSTANCE_ID,
            'uuid': pod_id},
        ports=[kube.client.V1ServicePort(port=80, target_port=8888)])
    service_template = kube.client.V1Service(
        metadata=kube.client.V1ObjectMeta(
            generate_name='notebook-worker-service-',
            labels={
                'app': 'notebook-worker',
                'hail.is/notebook-instance': INSTANCE_ID,
                'uuid': pod_id}),
        spec=service_spec)
    svc = k8s.create_namespaced_service(
        'default',
        service_template,
        _request_timeout=KUBERNETES_TIMEOUT_IN_SECONDS
    )
    pod_spec = kube.client.V1PodSpec(
        containers=[
            kube.client.V1Container(
                command=[
                    'jupyter',
                    'notebook',
                    f'--NotebookApp.token={jupyter_token}',
                    f'--NotebookApp.base_url=/instance/{svc.metadata.name}/'
                ],
                name='default',
                image=image,
                ports=[kube.client.V1ContainerPort(container_port=8888)],
                resources=kube.client.V1ResourceRequirements(
                    requests={'cpu': '1.601', 'memory': '1.601G'}),
                readiness_probe=kube.client.V1Probe(
                    http_get=kube.client.V1HTTPGetAction(
                        path=f'/instance/{svc.metadata.name}/login',
                        port=8888)))])
    pod_template = kube.client.V1Pod(
        metadata=kube.client.V1ObjectMeta(
            generate_name='notebook-worker-',
            labels={
                'app': 'notebook-worker',
                'hail.is/notebook-instance': INSTANCE_ID,
                'uuid': pod_id,
            }),
        spec=pod_spec)
    pod = k8s.create_namespaced_pod(
        'default',
        pod_template,
        _request_timeout=KUBERNETES_TIMEOUT_IN_SECONDS)
    return svc, pod


def external_url_for(path):
    # NOTE: nginx strips https and sets X-Forwarded-Proto: https, but
    # it is not used by request.url or url_for, so rewrite the url and
    # set _scheme='https' explicitly.
    protocol = request.headers.get('X-Forwarded-Proto', None)
    url = flask.url_for('root', _scheme=protocol, _external='true')
    return url + path


def forbidden():
    return 'Forbidden', 403


@app.route('/healthcheck')
def healthcheck():
    return '', 200

##### New notebook methods meant for consumption from agnostic client#####
###### Methods that do not call the auth-gateway /verify method must not be directly public #######
@app.route('/verify/<svc_name>/', methods=['GET'])
def validate_get(svc_name):
    access_token = request.args.get('authorization')
    token = request.args.get('token')

    if not access_token:
        return forbidden()

    resp = requests.get(f'http://{AUTH_GATEWAY}/verify',
                        headers={'Authorization': f'Bearer {access_token}'})

    if resp.status_code != 200:
        return forbidden()

    user_id = resp.headers.get('User')

    if not user_id:
        return forbidden()

    try:
        mysql_conn = pymysql.connect(
        host=IP, port=PORT, user=USER, passwd=PASS, db=DB)

        with mysql_conn.cursor() as cur:
            cur.execute(f"SELECT svc_name FROM sessions WHERE user_id='{user_id}' AND token='{token}'")

            rows = cur.fetchall()

            if len(rows) != 1 or rows[0][0] != svc_name:
                return forbidden()

            return '', 200
    except:
        log.exception("/validate : GET : failed")
        return '', 500


@app.route('/api', methods=['GET'])
def new_api_get():
    user_id = request.headers.get('User')

    if not user_id:
        return forbidden()

    try:
        mysql_conn = pymysql.connect(
        host=IP, port=PORT, user=USER, passwd=PASS, db=DB)

        sql = f"SELECT name, svc_name, token FROM {TABLE} WHERE user_id='{user_id}' AND state<>'{DELETED_STATE}'"

        rows = []
        with mysql_conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()

        return jsonify(rows), 200
    except Exception as e:
        log.exception(e)
        return '', 500


@app.route('/api/<token>', methods=['DELETE'])
def delete_notebook(token):
    user_id = request.headers.get("User")

    # TODO: Is it possible to have a falsy token value and get here?
    if not user_id or not token:
        forbidden()

    sqlDel = f"UPDATE {TABLE} SET state='{DELETED_STATE}' WHERE user_id='{user_id}' AND token='{token}'"
    sqlFetch = f"SELECT svc_name, pod_name FROM {TABLE} WHERE user_id='{user_id}' AND token='{token}'"

    try:
        mysql_conn = pymysql.connect(
            host=IP, port=PORT, user=USER, passwd=PASS, db=DB)

        with mysql_conn.cursor() as cur:
            rows_affected = cur.execute(sqlDel)

            if rows_affected != 1:
                log.exception(
                    f'/api/<token> : DELETE : user_id {user_id} has multiple entries for token {token}')

            cur.execute(sqlFetch)

            svc_name, pod_name = cur.fetchone()

            print("svc_name, pod_name", svc_name, pod_name)
            delete_worker_pod(pod_name, svc_name)

        mysql_conn.commit()
        # If deletion operation fails because record doesn't exist,
        # don't tell the user; they could exploit this

        return '', 200

    except Exception as e:
        log.exception(e)
        return '', 500


@app.route('/api', methods=['POST'])
def new_notebook():
    name = pymysql.escape_string(request.form.get('name', 'A notebook'))

    image = pymysql.escape_string(request.form['image'])

    user_id = request.headers.get('User')

    if not user_id or image not in WORKER_IMAGES:
        return forbidden()

    try:
        token = uuid.uuid4().hex  # FIXME: probably should be cryptographically secure
        svc, pod = start_pod(token, WORKER_IMAGES[image])

        svc_name = svc.metadata.name
        pod_name = pod.metadata.name

        sql = f"INSERT INTO {TABLE} (user_id,token,pod_name,svc_name,name,state) VALUES('{user_id}','{token}','{pod_name}','{svc_name}','{name}','{CREATED_STATE}')"

        mysql_conn = pymysql.connect(
            host=IP, port=PORT, user=USER, passwd=PASS, db=DB)

        with mysql_conn.cursor() as cur:
            cur.execute(sql)

        mysql_conn.commit()

        return jsonify([name, svc_name, token])

    except Exception as e:
        log.exception(e)
        return '', 500

##### Old notebook methods #####


@app.route('/')
def root():
    if 'svc_name' not in session:
        log.info(f'no svc_name found in session {session.keys()}')
        return render_template('index.html',
                               form_action_url=external_url_for('new'),
                               images=list(WORKER_IMAGES),
                               default='hail')
    svc_name = session['svc_name']
    jupyter_token = session['jupyter_token']
    log.info('redirecting to '
             + external_url_for(f'instance/{svc_name}/?token={jupyter_token}'))
    return redirect(external_url_for(f'instance/{svc_name}/?token={jupyter_token}'))


@app.route('/new', methods=['GET'])
def new_get():
    pod_name = session.get('pod_name')
    svc_name = session.get('svc_name')
    if pod_name:
        delete_worker_pod(pod_name, svc_name)
    session.clear()
    return redirect(external_url_for('/'))


@app.route('/new', methods=['POST'])
def new_post():
    log.info('new received')
    password = request.form['password']
    image = request.form['image']
    if password != PASSWORD or image not in WORKER_IMAGES:
        return '403 Forbidden', 403
    # FIXME: probably should be cryptographically secure
    jupyter_token = uuid.uuid4().hex
    svc, pod = start_pod(jupyter_token, WORKER_IMAGES[image])
    session['svc_name'] = svc.metadata.name
    session['pod_name'] = pod.metadata.name
    session['jupyter_token'] = jupyter_token
    return redirect(external_url_for(f'wait'))


@app.route('/wait', methods=['GET'])
def wait_webpage():
    return render_template('wait.html')


@app.route('/auth/<requested_svc_name>')
def auth(requested_svc_name):
    approved_svc_name = session.get('svc_name')
    if approved_svc_name and approved_svc_name == requested_svc_name:
        return '', 200
    return '', 403


def get_all_workers():
    workers = k8s.list_namespaced_pod(
        namespace='default',
        watch=False,
        label_selector='app=notebook-worker',
        _request_timeout=KUBERNETES_TIMEOUT_IN_SECONDS)
    workers_and_svcs = []
    for w in workers.items:
        uuid = w.metadata.labels['uuid']
        svcs = k8s.list_namespaced_service(
            namespace='default',
            watch=False,
            label_selector='uuid=' + uuid,
            _request_timeout=KUBERNETES_TIMEOUT_IN_SECONDS).items
        assert len(svcs) <= 1
        if len(svcs) == 1:
            workers_and_svcs.append((w, svcs[0]))
        else:
            log.info(f'assuming pod {w.metadata.name} is getting deleted '
                     f'because it has no service')
    return workers_and_svcs


@app.route('/workers')
def workers():
    if not session.get('admin'):
        return redirect(external_url_for('admin-login'))
    workers_and_svcs = get_all_workers()
    return render_template('workers.html',
                           workers=workers_and_svcs,
                           workers_url=external_url_for('workers'),
                           leader_instance=INSTANCE_ID)


@app.route('/workers/<pod_name>/<svc_name>/delete')
def workers_delete(pod_name, svc_name):
    if not session.get('admin'):
        return redirect(external_url_for('admin-login'))
    delete_worker_pod(pod_name, svc_name)
    return redirect(external_url_for('workers'))


@app.route('/workers/delete-all-workers', methods=['POST'])
def delete_all_workers():
    if not session.get('admin'):
        return redirect(external_url_for('admin-login'))
    workers_and_svcs = get_all_workers()
    for pod_name, svc_name in workers_and_svcs:
        delete_worker_pod(pod_name, svc_name)
    return redirect(external_url_for('workers'))


def delete_worker_pod(pod_name, svc_name):
    try:
        k8s.delete_namespaced_pod(
            pod_name,
            'default',
            kube.client.V1DeleteOptions(),
            _request_timeout=KUBERNETES_TIMEOUT_IN_SECONDS)
    except kube.client.rest.ApiException as e:
        log.info(f'pod {pod_name} or associated service already deleted {e}')
    try:
        k8s.delete_namespaced_service(
            svc_name,
            'default',
            kube.client.V1DeleteOptions(),
            _request_timeout=KUBERNETES_TIMEOUT_IN_SECONDS)
    except kube.client.rest.ApiException as e:
        log.info(
            f'service {svc_name} (for pod {pod_name}) already deleted {e}')


@app.route('/admin-login', methods=['GET'])
def admin_login():
    return render_template('admin-login.html',
                           form_action_url=external_url_for('admin-login'))


@app.route('/admin-login', methods=['POST'])
def admin_login_post():
    if request.form['password'] != ADMIN_PASSWORD:
        return '403 Forbidden', 403
    session['admin'] = True
    return redirect(external_url_for('workers'))


@app.route('/worker-image')
def worker_image():
    return '\n'.join(WORKER_IMAGES.values()), 200


@sockets.route('/wait')
def wait_websocket(ws):
    pod_name = session['pod_name']
    svc_name = session['svc_name']
    jupyter_token = session['jupyter_token']
    log.info(f'received wait websocket for {svc_name} {pod_name}')
    # wait for instance ready
    while True:
        try:
            response = requests.head(f'https://notebook.hail.is/instance-ready/{svc_name}/',
                                     timeout=1)
            if response.status_code < 500:
                log.info(
                    f'HEAD on jupyter succeeded for {svc_name} {pod_name} response: {response}')
                # if someone responds with a 2xx, 3xx, or 4xx, the notebook
                # server is alive and functioning properly (in particular, our
                # HEAD request will return 405 METHOD NOT ALLOWED)
                break
            else:
                # somewhat unusual, means the gateway had an error before we
                # timed out, usually means the gateway itself is broken
                log.info(
                    f'HEAD on jupyter failed for {svc_name} {pod_name} response: {response}')
                gevent.sleep(1)
            break
        except requests.exceptions.Timeout as e:
            log.info(f'GET on jupyter failed for {svc_name} {pod_name}')
            gevent.sleep(1)
    ws.send(external_url_for(f'instance/{svc_name}/?token={jupyter_token}'))
    log.info(f'notification sent to user for {svc_name} {pod_name}')


if __name__ == '__main__':
    from gevent import pywsgi
    from geventwebsocket.handler import WebSocketHandler
    server = pywsgi.WSGIServer(
        ('', 5000), app, handler_class=WebSocketHandler, log=log)
    server.serve_forever()
