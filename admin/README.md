# Wget Admin

Web-based admin panel for GNU Wget - clone websites with a modern UI.

## Features

- Clone websites with customizable options
- Real-time progress monitoring via WebSocket
- Download results as ZIP archive
- Open cloned sites in browser
- Advanced options: depth, rate limit, user-agent, filters

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
python app.py
```

Open http://127.0.0.1:5050 in your browser.

## Configuration

Set `WGET_PATH` environment variable to use a custom wget binary:

```bash
export WGET_PATH=/path/to/wget
python app.py
```

## Requirements

- Python 3.8+
- GNU Wget

## License

MIT
