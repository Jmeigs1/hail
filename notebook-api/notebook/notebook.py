"""
A Jupyter notebook service with local-mode Hail pre-installed
"""
import gevent
# must happen before anytyhing else
from gevent import monkey, pywsgi
from geventwebsocket.handler import WebSocketHandler
monkey.patch_all()

import requests
import ujson
from flask import Flask, request, Response
from flask_sockets import Sockets
import flask
import kubernetes as kube
import logging
import os
import re
import requests
import time
import uuid
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

# Prevent issues with many websockets leading to many kube connections
# TODO: tune this
# TODO: probably remove once asynchttp used, re-use one connection, watch all pods, yield if no websockets in global for user
kube.config.connection_pool_maxsize = 5000
k8s = kube.client.CoreV1Api()

app = Flask(__name__)
sockets = Sockets(app)


def read_string(f):
    with open(f, 'r') as f:
        return f.read().strip()


AUTH_GATEWAY = os.environ.get("AUTH_GATEWAY", "http://auth-gateway")
HAIL_IMAGE = os.environ.get("HAIL_IMAGE", "hail-jupyter")

KUBERNETES_TIMEOUT_IN_SECONDS = float(
    os.environ.get('KUBERNETES_TIMEOUT_IN_SECONDS', 5.0))
INSTANCE_ID = uuid.uuid4().hex

# used for /verify; will likely go away once 2nd (notebook) verify step moved to nginx
log.info(f'AUTH_GATEWAY: {AUTH_GATEWAY}')
log.info(f'HAIL_IMAGE: {HAIL_IMAGE}')
log.info(f'KUBERNETES_TIMEOUT_IN_SECONDS: {KUBERNETES_TIMEOUT_IN_SECONDS}')
log.info(f'INSTANCE_ID: {INSTANCE_ID}')

try:
    with open('notebook-worker-images', 'r') as f:
        def get_name(line):
            return re.search("/([^/:]+):", line).group(1)
        WORKER_IMAGES = {get_name(line): line.strip() for line in f}
except FileNotFoundError as e:
    raise ValueError(
        "working directory must contain a file called `notebook-worker-images' "
        "containing the name of the docker image to use for worker pods.") from e


#################### Kube resource maangement #########################


# A basic transformation to make user_ids safe for Kube
# TODO: use hash
def UNSAFE_user_id_transform(user_id): return user_id.replace('|', '--_--')


def start_pod(jupyter_token, image, labels={}):
    pod_id = uuid.uuid4().hex

    pod_spec = kube.client.V1PodSpec(
        containers=[
            kube.client.V1Container(
                command=[
                    'jupyter',
                    'notebook',
                    "--ip", "0.0.0.0", "--no-browser",
                    f'--NotebookApp.token={jupyter_token}',
                    f'--NotebookApp.base_url=/instance/{pod_id}/'
                ],
                name='default',
                image=image,
                ports=[kube.client.V1ContainerPort(container_port=8888)],
                resources=kube.client.V1ResourceRequirements(
                     requests={'cpu': '1.601', 'memory': '1.601G'}),
                readiness_probe=kube.client.V1Probe(
                    http_get=kube.client.V1HTTPGetAction(
                        path=f'/instance/{pod_id}/login',
                        port=8888)))])
    pod_template = kube.client.V1Pod(
        metadata=kube.client.V1ObjectMeta(
            generate_name='notebook-worker-',
            labels={
                'app': 'notebook-worker',
                'hail.is/notebook-instance': INSTANCE_ID,
                'uuid': pod_id,
                **labels
            },),
        spec=pod_spec)
    pod = k8s.create_namespaced_pod(
        'default',
        pod_template,
        _request_timeout=KUBERNETES_TIMEOUT_IN_SECONDS,
    )

    return pod


def del_pod(pod_name):
    k8s.delete_namespaced_pod(
        pod_name,
        'default',
        kube.client.V1DeleteOptions(),
        _request_timeout=KUBERNETES_TIMEOUT_IN_SECONDS)


def del_svc(svc_name):
    k8s.delete_namespaced_service(
        svc_name,
        'default',
        kube.client.V1DeleteOptions(),
        _request_timeout=KUBERNETES_TIMEOUT_IN_SECONDS)


###################### General resource marshalling functions ###################


def get_path(data, path: str):
    # Not using tail recursion because python doesn't optimize such calls
    while True:
        idx = path.find('.')

        if idx == -1:
            if isinstance(data, dict):
                return data[path]
            return getattr(data, path)

        data = getattr(data, path[0:idx])

        path = path[idx + 1:]


# Given a kubernetes object, and some collection of field paths, walk path tree and fetch values
# @param resources<List> : kubernetes v1 object
# @param paths<List<List[3]>> : fields and transformations: [key, path_in_kube_object, lambda_for_found_val]


def marshall_json(resources: [], paths=[], flatten=False):
    if len(resources) == 0 or len(paths) == 0:
        if flatten == True:
            return "{}"

        return "[]"

    resp = []
    for rsc in resources:
        data = {}
        for path in paths:
            if len(path) == 3:
                data[path[0]] = path[2](get_path(rsc, path[1]))
            else:
                data[path[0]] = get_path(rsc, path[1])

        resp.append(data)

    if flatten == True and len(resources) == 1:
        return ujson.dumps(resp[0])

    return ujson.dumps(resp)


#################### Pod resource marshalling path functions ###################


def read_svc_status(svc_name):
    try:
        # TODO: inspect exception for non-404
        _ = k8s.read_namespaced_service(svc_name, 'default')
        return 'Running'
    except:
        return 'Deleted'


def read_containers_status(container_statuses):
    if container_statuses is None:
        return None

    state = container_statuses[0].state
    rn = None
    wt = None
    tm = None
    if state.running:
        rn = {"started_at": state.running.started_at}

    if state.waiting:
        wt = {"reaason": state.waiting.reason}

    if state.terminated:
        tm = {"exit_code": state.terminated.exit_code, "finished_at": state.terminated.finished_at,
              "started_at": state.terminated.started_at, "reason": state.terminated.reason}

    if rn is None and wt is None and tm is None:
        return None

    return {"running": rn, "terminated": tm, "waiting": wt}


def read_conditions(conds):
    if conds is None:
        return None

    maxDate = None
    maxCond = None
    for condition in conds:
        if maxDate is None:
            maxCond = condition
            maxDate = condition.last_transition_time
            continue

        if condition.last_transition_time > maxDate and condition.status == "True":
            maxCond = condition
            maxDate = condition.last_transition_time

    # 'message': 'containers with unready status: [default]',
    #              'reason': 'ContainersNotReady',
    #              'status': 'False',
    #              'type': 'Ready'
    return {"message": maxCond.message, "reason": maxCond.reason, "status": maxCond.status, "type": maxCond.type}


pod_paths = [
    ['name', 'metadata.labels.name'],
    ['pod_name', 'metadata.name'],
    ['svc_name', 'metadata.labels.svc_name'],
    ['pod_status', 'status.phase'],
    ['creation_date', 'metadata.creation_timestamp',
        lambda x: x.strftime('%D')],
    ['token', 'metadata.labels.jupyter_token'],
    ['container_status', 'status.container_statuses',
        lambda x: read_containers_status(x)],
    ['condition', 'status.conditions', lambda x: read_conditions(x)]
]

# pod_paths = [
#     *common_pod_paths,
#     ['svc_status', 'metadata.labels.svc_name', lambda x: read_svc_status(x)],
# ]

# # TODO: This may not work in all cases; we may need to pass the svc object
# # and check it; it doesn't seem to have useful status information
# pod_paths_post = [
#     *common_pod_paths,
#     ['svc_status', 'metadata.labels.svc_name', lambda _: 'Running'],
# ]

########################## WS and HTTP Routes ##################################


# TODO: learn how to properly handle webscocket close in gevent + wsgi
# or just move to aiohttp and stop dealing with greenlets and websockets with
# no bound events (though there may be a way in gevent's websocket package + wsgi)
# simply checkingfo ws.close isn't enough
# https://github.com/heroku-python/flask-sockets/issues/60
# A reactive approach to websockets. Primary benefit is 1 connection per user
# rather than 1 connection per pod
# no need to go to public web to hit http endpoint
# and real insight into pod status
# Weakness is currently not checking whether svc is accessible; easily can add

def forbidden():
    return 'Forbidden', 404


@sockets.route('/api/ws')
def echo_socket(ws):
    user_id = ws.environ['HTTP_USER']  # and scope is ws.environ['HTTP_SCOPE']
    w = kube.watch.Watch()

    for event in w.stream(k8s.list_namespaced_pod, namespace='default',
                          label_selector=f"user_id={UNSAFE_user_id_transform(user_id)}"):

        # This won't prevent socket is dead errors, presumably something related to
        # greenlet socket handling and our inability to add an on_closed callback to end watch
        if ws.closed:
            log.info("Websocket closed")
            w.stop()
            return

        try:
            obj = event["object"]

            if event['type'] == 'MODIFIED' or event['type'] == 'ADDED':
                ws.send(
                    f'{{"event": "{event["type"]}", "resource": {marshall_json([obj], pod_paths, True)}}}')

            if event['type'] == 'DELETED':
                ws.send(
                    f'{{"event": "DELETED", "resource": {marshall_json([obj], pod_paths, True)}}}')

        except Exception as e:
            log.info("Issue with watcher")
            log.error(e)
            w.stop()
            break

    ws.close()


@app.route('/api/verify/<pod_name>/', methods=['GET'])
def verify(pod_name):
    access_token = request.cookies.get('access_token')
    # No longer verify the juptyer token; let jupyter handle this
    # The URI gets modified by jupyter, so get queries get lost
    # Since the token is just a jupyter password, we can skip this
    # and simply verify the user owns the resource (here svc_name)
    # token = request.args.get('token')
    # log.info(f'JUPYTER TOKEN: {token}')

    if not access_token:
        return '', 401

    resp = requests.get(f'{AUTH_GATEWAY}/verify',
                        headers={'Authorization': f'Bearer {access_token}'})

    if resp.status_code != 200:
        return '', 401

    user_id = resp.headers.get('User')

    if not user_id:
        return '', 401

    k_res = k8s.read_namespaced_pod(pod_name, 'default')
    print('stuff', k_res)

    l = k_res.metadata.labels

    if l['user_id'] != UNSAFE_user_id_transform(user_id):
        return '', 401

    resp = Response('')
    # resp.headers['IP'] = k_res.spec.cluster_ip

    return resp


@app.route('/api', methods=['GET'])
def get_notebooks():
    user_id = request.headers.get('User')

    if not user_id:
        return forbidden()

    pods = k8s.list_namespaced_pod(
        namespace='default',
        label_selector=f"user_id={UNSAFE_user_id_transform(user_id)}", timeout_seconds=30).items

    return marshall_json(pods, pod_paths), 200

# TODO: decide if need to communicate issues to user; probably just alert devs


@app.route('/api/<pod_name>', methods=['DELETE'])
def delete_notebook(pod_name):
    # TODO: Is it possible to have a falsy token value and get here?
    if not request.headers.get("User") or not pod_name:
        return forbidden()

    escp_user_id = UNSAFE_user_id_transform(request.headers.get("User"))

    try:
        pod = k8s.read_namespaced_pod(
            pod_name, 'default', _request_timeout=30).items

        if pod.metadata.labels['user_id'] != escp_user_id:
            return forbidden()

        del_pod(pod.metadata.name)

        return '', 200

    except kube.client.rest.ApiException as e:
        # TODO: enable this; front-end lightweight fetch library makes it difficult to recover from trivial errors
        # There must be a nicer way to read http error code from kubernetes
        # return '', str(e)[1:4]
        return '', 200


@app.route('/api', methods=['POST'])
def new_notebook():
    name = request.form.get('name', 'a_notebook')
    image = request.form.get('image', HAIL_IMAGE)

    user_id = request.headers.get('User')

    if not user_id or image not in WORKER_IMAGES:
        return forbidden()

    # TODO: Do we want jupyter_token to be globally unique?
    # Token doesn't need to be crypto secure, just unique (since we authorize user)
    # However, encryption or even detection of modification via hash may further reduce attack space
    jupyter_token = uuid.uuid4().hex
    pod = start_pod(
        jupyter_token, WORKER_IMAGES[image],
        labels={'name': name,
                'jupyter_token': jupyter_token,
                'user_id': UNSAFE_user_id_transform(user_id)}
    )

    # We could also construct pod_paths_post here, and close over svc
    return marshall_json([pod], pod_paths, True), 200


if __name__ == '__main__':

    server = pywsgi.WSGIServer(
        ('', 5000), app, handler_class=WebSocketHandler, log=log)

    server.serve_forever()
