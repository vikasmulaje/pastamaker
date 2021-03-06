# -*- encoding: utf-8 -*-
#
# Copyright © 2017 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import gevent
import gevent.monkey
gevent.monkey.patch_all()

import hmac
import logging
import os

import flask
import github
import lz4.block
import rq
import ujson

from pastamaker import config
from pastamaker import utils
from pastamaker import worker


LOG = logging.getLogger(__name__)

app = flask.Flask(__name__)


def get_redis():
    if not hasattr(flask.g, 'redis'):
        conn = utils.get_redis()
        flask.g.redis = conn
    return flask.g.redis


def get_queue():
    if not hasattr(flask.g, 'rq_queue'):
        flask.g.rq_queue = rq.Queue(connection=get_redis())
    return flask.g.rq_queue


@app.route("/auth", methods=["GET"])
def auth():
    return "pastamaker don't need oauth setup"


@app.route("/refresh/<owner>/<repo>/<path:branch>",
           methods=["POST"])
def refresh(owner, repo, branch):

    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)

    installation_id = utils.get_installation_id(integration, owner)
    if not installation_id:
        flask.abort(404, "%s have not installed pastamaker" % owner)

    # Mimic the github event format
    data = {
        'repository': {
            'name': repo,
            'full_name': '%s/%s' % (owner, repo),
            'owner': {'login': owner},
        },
        'installation': {'id': installation_id},
        "branch": branch,
    }
    get_queue().enqueue(worker.event_handler, "refresh", data)
    return "", 202


@app.route("/refresh", methods=["POST"])
def refresh_all():
    authentification()

    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)

    counts = [0, 0, 0]
    for install in utils.get_installations(integration):
        counts[0] += 1
        token = integration.get_access_token(install["id"]).token
        g = github.Github(token)
        i = g.get_installation(install["id"])

        for repo in i.get_repos():
            counts[1] += 1
            pulls = repo.get_pulls()
            branches = set([p.base.ref for p in pulls])

            # Mimic the github event format
            for branch in branches:
                counts[2] += 1
                get_queue().enqueue(worker.event_handler, "refresh", {
                    'repository': repo.raw_data,
                    'installation': {'id': install['id']},
                    'branch': branch,
                })
    return ("Updated %s installations, %s repositories, "
            "%s branches" % tuple(counts)), 202


@app.route("/queue/<owner>/<repo>/<path:branch>")
def queue(owner, repo, branch):
    return get_redis().get("queues~%s~%s~%s" % (owner, repo, branch)) or "[]"


def _get_status(r):
    queues = []
    for key in r.keys("queues~*~*~*"):
        _, owner, repo, branch = key.split("~")
        updated_at = None

        payload = r.get(key)
        if payload:
            payload = lz4.block.decompress(payload)
            pulls = ujson.loads(payload)
            updated_at = list(sorted([p["updated_at"] for p in pulls]))[-1]
        queues.append({
            "owner": owner,
            "repo": repo,
            "branch": branch,
            "pulls": pulls,
            "updated_at": updated_at,
        })
    return ujson.dumps(queues)


@app.route("/status")
def status():
    r = get_redis()
    return _get_status(r)


def stream_message(_type, data):
    return 'event: %s\ndata: %s\n\n' % (_type, data)


def stream_generate():
    r = get_redis()
    yield stream_message("refresh", _get_status(r))
    pubsub = r.pubsub()
    pubsub.subscribe("update")
    while True:
        # NOTE(sileht): heroku timeout is 55s, we have set gunicorn timeout to
        # 60s, this assume 5s is enough for http and redis round strip and use
        # 50s
        message = pubsub.get_message(timeout=50.0)
        if message is None:
            yield stream_message("ping", "{}")
        elif message["channel"] == "update":
            yield stream_message("refresh", _get_status(r))


@app.route('/status/stream')
def stream():
    return flask.Response(flask.stream_with_context(stream_generate()),
                          mimetype="text/event-stream")


@app.route("/event", methods=["POST"])
def event_handler():
    authentification()

    event_type = flask.request.headers.get("X-GitHub-Event")
    event_id = flask.request.headers.get("X-GitHub-Delivery")
    data = flask.request.get_json()

    if event_type in ["refresh", "pull_request", "status",
                      "pull_request_review"]:
        get_queue().enqueue(worker.event_handler, event_type, data)

    if "repository" in data:
        repo_name = data["repository"]["full_name"]
    else:
        repo_name = data["installation"]["account"]["login"]

    LOG.info('[%s/%s] received "%s" event "%s"',
             data["installation"]["id"], repo_name,
             event_type, event_id)

    return "", 202


@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/favicon.ico")
def favicon():
    return app.send_static_file("favicon.ico")


@app.route("/fonts/<file>")
def fonts(file):
    # bootstrap fonts
    return flask.send_from_directory(os.path.join("static", "fonts"), file)


def authentification():
    # Only SHA1 is supported
    header_signature = flask.request.headers.get('X-Hub-Signature')
    if header_signature is None:
        LOG.warning("Webhook without signature")
        flask.abort(403)

    try:
        sha_name, signature = header_signature.split('=')
    except ValueError:
        sha_name = None

    if sha_name != 'sha1':
        LOG.warning("Webhook signature malformed")
        flask.abort(403)

    mac = utils.compute_hmac(flask.request.data)
    if not hmac.compare_digest(mac, str(signature)):
        LOG.warning("Webhook signature invalid")
        flask.abort(403)
