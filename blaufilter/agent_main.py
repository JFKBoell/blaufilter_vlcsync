from __future__ import annotations

import click
from waitress import serve

from blaufilter import config as bf_config
from blaufilter.agent import create_agent_app


@click.command
@click.option("--port", "port", type=int, default=None, help="Agent listen port (default 4213).")
@click.option("--config-path", "config_path", default=bf_config.CONFIG_PATH, show_default=True)
def main(port, config_path):
    """Blaufilter agent: accepts video pushes and restarts local VLC."""
    cfg = bf_config.load(config_path)
    listen_port = port if port is not None else cfg.agent_port
    app = create_agent_app(cfg.video_path, cfg.vlc_unit)
    print(f"Blaufilter agent on 0.0.0.0:{listen_port} video={cfg.video_path}", flush=True)
    serve(app, host="0.0.0.0", port=listen_port, threads=4)


if __name__ == "__main__":
    main()
