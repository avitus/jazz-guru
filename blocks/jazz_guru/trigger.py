"""Trigger a smoke test against the running jazz_guru agent.

Sends `{ skill: "chat", message: "..." }` and prints the JSON envelope
the handler returns. Edit `payload` below to exercise other skills:

    {"skill": "distill", "session_id": "<uuid>"}
    {"skill": "evalrun"}
    {"skill": "render_midi", "midi_path": "in.mid", "out_path": "out.wav",
     "instrument": "jazz_organ"}
"""

import base64
import json
import threading

from blocks_network import SendMessageRequestPart, create_task_client


def main():
    payload = {
        "skill": "chat",
        "message": "Ping from the Blocks trigger script. List your tools.",
    }

    client = create_task_client()

    session = client.send_message(
        agent_name="jazz_guru",
        request_parts=[
            SendMessageRequestPart(part_id="request", text=json.dumps(payload))
        ],
    )

    print(f"Task created: {session.task_id}")

    done = threading.Event()

    def on_progress(event):
        print("[progress]", event.get("message") or event.get("progress") or "")

    def on_artifact(event):
        ref = event.artifact_ref
        if ref is None:
            print("[artifact]", event.raw)
            return
        if ref.kind == "inline" and ref.data:
            text = base64.b64decode(ref.data).decode()
            print("[artifact]", text)
        else:
            downloaded = session.download_artifact(ref)
            print("[artifact]", downloaded.data.decode())

    def on_terminal(event):
        print("[done] Task complete")
        done.set()

    session.on_progress(on_progress)
    session.on_artifact(on_artifact)
    session.on_terminal(on_terminal)

    done.wait(timeout=120)
    session.close()
    client.destroy()


if __name__ == "__main__":
    main()
