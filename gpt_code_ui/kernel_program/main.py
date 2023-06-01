import os
import subprocess
import sys
import pathlib
import json
import logging
import time

import asyncio
import json
import threading

from queue import Queue

from flask import Flask, request, jsonify
from flask_cors import CORS  # Import the CORS library

from dotenv import load_dotenv
load_dotenv('.env')

import gpt_code_ui.kernel_program.kernel_manager as kernel_manager
import gpt_code_ui.kernel_program.config as config
import gpt_code_ui.kernel_program.utils as utils


APP_PORT = int(os.environ.get("API_PORT", 5010))

# Get global logger
logger = config.get_logger()

# Note, only one kernel_manager_process can be active
kernel_manager_process = None

# Use efficient Python queues to store messages
result_queues = {"all": Queue()}
# result_queue = Queue()
send_queue = Queue()

messaging = None

# We know this Flask app is for local use. So we can disable the verbose Werkzeug logger
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

cli = sys.modules['flask.cli']
cli.show_server_banner = lambda *x: None

app = Flask(__name__)
CORS(app)


def start_kernel_manager():
    global kernel_manager_process

    kernel_manager_script_path = os.path.join(
        pathlib.Path(__file__).parent.resolve(), "kernel_manager.py"
    )
    kernel_manager_process = subprocess.Popen(
        [sys.executable, kernel_manager_script_path]
    )

    # Write PID as <pid>.pid to config.KERNEL_PID_DIR
    os.makedirs(config.KERNEL_PID_DIR, exist_ok=True)
    with open(os.path.join(config.KERNEL_PID_DIR, "%d.pid" % kernel_manager_process.pid), "w") as p:
        p.write("kernel_manager")


def cleanup_kernel_program():
    kernel_manager.cleanup_spawned_processes()


async def start_snakemq():
    global messaging

    messaging, link = utils.init_snakemq(config.IDENT_MAIN)

    def on_recv(conn, ident, message):
        message = json.loads(message.data.decode("utf-8"))
        source = message["source"] if "source" in message else None
        queue_key = "all" if source is None else source

        if queue_key not in result_queues.keys():
            result_queues[queue_key] = Queue()

        if message["type"] == "status":
            if message["value"] == "ready":
                logger.debug("Kernel is ready.")
                result_queues[queue_key].put({
                    "value": "Kernel is ready.",
                    "type": "message",
                    "source": source
                })

        elif message["type"] in ["message", "message_raw", "image/png", "image/jpeg"]:
            # TODO: 1:1 kernel <> channel mapping
            logger.debug("%s of type %s" % (message["value"], message["type"]))
            result_queues[queue_key].put({
                "value": message["value"],
                "type": message["type"],
                "source": source
            })

    messaging.on_message_recv.add(on_recv)
    logger.info("Starting snakemq loop")

    def send_queued_messages():
        while True:
            if send_queue.qsize() > 0:
                message = send_queue.get()
                source = message["source"] if "source" in message else None
                utils.send_json(messaging,
                    {"type": "execute", "value": message["command"], "source": source},
                    config.IDENT_KERNEL_MANAGER
                )
            time.sleep(0.1)

    async def async_send_queued_messages():
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, send_queued_messages)

    async def async_link_loop():
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, link.loop)

    # Wrap the snakemq_link.Link loop in an asyncio task
    await asyncio.gather(async_send_queued_messages(), async_link_loop())


@app.route("/api", methods=["POST", "GET"])
def handle_request():
    token = request.headers.get("Token", None)

    if request.method == "GET":
        # Handle GET requests by sending everything that's in the receive_queue
        queue_key = "all" if token is None else token

        if queue_key not in result_queues.keys():
            result_queues[queue_key] = Queue()

        for _ in range(result_queues[queue_key].qsize()):
            print("Here:", result_queues["all"].get())

        results = [result_queues[queue_key].get() for _ in range(result_queues[queue_key].qsize())]

        return jsonify({"results": results})
    elif request.method == "POST":
        data = request.json
        data["source"] = token
        send_queue.put(data)

        return jsonify({"result": "success"})


@app.route("/restart", methods=["POST"])
def handle_restart():

    cleanup_kernel_program()
    start_kernel_manager()

    return jsonify({"result": "success"})


async def main():
    start_kernel_manager()

    # Run Flask app in a separate thread
    flask_thread = threading.Thread(target=run_flask_app)
    flask_thread.start()

    # Run in background
    await start_snakemq()


def run_flask_app():
    app.run(host="0.0.0.0", port=APP_PORT)


if __name__ == "__main__":
    asyncio.run(main())
