#!/usr/bin/env python
'''
THIS APP IS NOT PRODUCTION READY!! DO NOT USE!

Flask app that provides a RESTful API to
the multiscanner.

Proposed supported operations:
GET / ---> Test functionality. {'Message': 'True'}
GET /api/v1/tasks/list  ---> Receive list of tasks in multiscanner
GET /api/v1/tasks/list/<task_id> ---> receive task in JSON format
GET /api/v1/tasks/report/<task_id> ---> receive report in JSON
GET /api/v1/tasks/delete/<task_id> ----> delete task_id
POST /api/v1/tasks/create ---> POST file and receive report id
Sample POST usage:
    curl -i -X POST http://localhost:8080/api/v1/tasks/create/ -F file=@/bin/ls

The API endpoints all have Cross Origin Resource Sharing (CORS) enabled and set
to allow ALL origins.

TODO:
* Add doc strings to functions
'''
from __future__ import print_function
import os
import sys
import time
import hashlib
import configparser
import multiprocessing
import queue
from uuid import uuid4
from flask_cors import cross_origin
from flask import Flask, jsonify, make_response, request, abort

MS_WD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(MS_WD, 'storage') not in sys.path:
    sys.path.insert(0, os.path.join(MS_WD, 'storage'))
if MS_WD not in sys.path:
    sys.path.insert(0, os.path.join(MS_WD))

import multiscanner
import sql_driver as database
from storage import Storage
import elasticsearch_storage
from celery_worker import multiscanner_celery

TASK_NOT_FOUND = {'Message': 'No task with that ID found!'}
INVALID_REQUEST = {'Message': 'Invalid request parameters'}

BATCH_SIZE = 100
WAIT_SECONDS = 60   # Number of seconds to wait for additional files
                    # submitted to the create/ API

HTTP_OK = 200
HTTP_CREATED = 201
HTTP_BAD_REQUEST = 400
HTTP_NOT_FOUND = 404

DEFAULTCONF = {
    'host': 'localhost',
    'port': 8080,
    'upload_folder': '/mnt/samples/',
}

app = Flask(__name__)
api_config_object = configparser.SafeConfigParser()
api_config_object.optionxform = str
api_config_file = multiscanner.common.get_api_config_path(multiscanner.CONFIG)
api_config_object.read(api_config_file)
api_config = multiscanner.common.parse_config(api_config_object)

db = database.Database(config=api_config.get('Database'))
storage_conf = multiscanner.common.get_storage_config_path(multiscanner.CONFIG)
storage_handler = multiscanner.storage.StorageHandler(configfile=storage_conf)
for handler in storage_handler.loaded_storage:
    if isinstance(handler, elasticsearch_storage.ElasticSearchStorage):
        break


@app.errorhandler(HTTP_BAD_REQUEST)
def invalid_request(error):
    '''Return a 400 with the INVALID_REQUEST message.'''
    return make_response(jsonify(INVALID_REQUEST), HTTP_BAD_REQUEST)


@app.errorhandler(HTTP_NOT_FOUND)
def not_found(error):
    '''Return a 404 with a TASK_NOT_FOUND message.'''
    return make_response(jsonify(TASK_NOT_FOUND), HTTP_NOT_FOUND)


@app.route('/')
def index():
    '''
    Return a default standard message
    for testing connectivity.
    '''
    return jsonify({'Message': 'True'})


@app.route('/api/v1/tasks/list/', methods=['GET'])
@cross_origin()
def task_list():
    '''
    Return a JSON dictionary containing all the tasks
    in the DB.
    '''

    return jsonify({'Tasks': db.get_all_tasks()})


@app.route('/api/v1/tasks/list/<int:task_id>', methods=['GET'])
@cross_origin()
def get_task(task_id):
    '''
    Return a JSON dictionary corresponding
    to the given task ID.
    '''
    task = db.get_task(task_id)
    if task:
        return jsonify({'Task': task.to_dict()})
    else:
        abort(HTTP_NOT_FOUND)


@app.route('/api/v1/tasks/delete/<int:task_id>', methods=['GET'])
@cross_origin()
def delete_task(task_id):
    '''
    Delete the specified task. Return deleted message.
    '''
    result = db.delete_task(task_id)
    if not result:
        abort(HTTP_NOT_FOUND)
    return jsonify({'Message': 'Deleted'})


@app.route('/api/v1/tasks/create/', methods=['POST'])
@cross_origin()
def create_task():
    '''
    Create a new task. Save the submitted file
    to UPLOAD_FOLDER. Return task id and 201 status.
    '''
    file_ = request.files['file']
    original_filename = file_.filename
    f_name = hashlib.sha256(file_.read()).hexdigest()
    # Reset the file pointer to the beginning
    # to allow us to save it
    file_.seek(0)

    file_path = os.path.join(api_config['api']['upload_folder'], f_name)
    file_.save(file_path)
    full_path = os.path.join(MS_WD, file_path)

    # Add task to SQL task DB
    task_id = db.add_task()

    # Publish the task to Celery
    multiscanner_celery.delay(full_path, original_filename, task_id, f_name)

    return make_response(
        HTTP_CREATED
    )


@app.route('/api/v1/tasks/report/<task_id>', methods=['GET'])
@cross_origin()
def get_report(task_id):
    '''
    Return a JSON dictionary corresponding
    to the given task ID.
    '''
    task = db.get_task(task_id)
    if not task:
        abort(HTTP_NOT_FOUND)

    if task.task_status == 'Complete':
        report = handler.get_report(task.report_id)

    elif task.task_status == 'Pending':
        report = {'Report': 'Task still pending'}

    if report:
        return jsonify({'Report': report})
    else:
        abort(HTTP_NOT_FOUND)


@app.route('/api/v1/tasks/delete/<task_id>', methods=['GET'])
@cross_origin()
def delete_report(task_id):
    '''
    Delete the specified task. Return deleted message.
    '''
    task = db.get_task(task_id)
    if not task:
        abort(HTTP_NOT_FOUND)

    if handler.delete(task.report_id):
        return jsonify({'Message': 'Deleted'})
    else:
        abort(HTTP_NOT_FOUND)


@app.route('/api/v1/tags/', methods=['GET'])
@cross_origin()
def taglist():
    '''
    Return a list of all tags currently in use.
    '''
    response = handler.get_tags()
    if not response:
        abort(HTTP_BAD_REQUEST)
    return jsonify({'Tags': response})


@app.route('/api/v1/tasks/tags/<task_id>', methods=['GET'])
@cross_origin()
def tags(task_id):
    '''
    Add/Remove the specified tag to the specified task.
    '''
    task = db.get_task(task_id)
    if not task:
        abort(HTTP_NOT_FOUND)

    add = request.args.get('add', '')
    if add:
        response = handler.add_tag(task.sample_id, add)
        if not response:
            abort(HTTP_BAD_REQUEST)
        return jsonify({'Message': 'Tag Added'})

    remove = request.args.get('remove', '')
    if remove:
        response = handler.remove_tag(task.sample_id, remove)
        if not response:
            abort(HTTP_BAD_REQUEST)
        return jsonify({'Message': 'Tag Removed'})


@app.route('/api/v1/tasks/<task_id>/notes', methods=['GET'])
@cross_origin()
def get_notes(task_id):
    '''
    Add an analyst note/comment to the specified task.
    '''
    task = db.get_task(task_id)
    if not task:
        abort(HTTP_NOT_FOUND)

    if ('ts' in request.args and 'uid' in request.args):
        ts = request.args.get('ts', '')
        uid = request.args.get('uid', '')
        response = handler.get_notes(task.sample_id, [ts, uid])
    else:
        response = handler.get_notes(task.sample_id)

    if not response:
        abort(HTTP_BAD_REQUEST)

    if 'hits' in response and 'hits' in response['hits']:
        response = response['hits']['hits']
    return jsonify(response)


@app.route('/api/v1/tasks/<task_id>/note', methods=['POST'])
@cross_origin()
def add_note(task_id):
    '''
    Add an analyst note/comment to the specified task.
    '''
    task = db.get_task(task_id)
    if not task:
        abort(HTTP_NOT_FOUND)

    response = handler.add_note(task.sample_id, request.form.to_dict())
    if not response:
        abort(HTTP_BAD_REQUEST)
    return jsonify(response)


if __name__ == '__main__':

    db.init_db()

    if not os.path.isdir(api_config['api']['upload_folder']):
        print('Creating upload dir')
        os.makedirs(api_config['api']['upload_folder'])

    app.run(host=api_config['api']['host'], port=api_config['api']['port'])
