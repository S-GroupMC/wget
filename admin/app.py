#!/usr/bin/env python3
"""
Wget Web Admin - Web interface for GNU Wget
"""

import os
import subprocess
import threading
import uuid
import json
import shutil
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template, request, jsonify, send_from_directory, send_file
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['SECRET_KEY'] = 'wget-admin-secret-key'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Configuration
BASE_DIR = Path(__file__).parent.parent
WGET_PATH = os.environ.get('WGET_PATH', str(BASE_DIR / 'src' / 'wget'))
WGET2_PATH = os.environ.get('WGET2_PATH', '/opt/homebrew/bin/wget2')
DOWNLOADS_DIR = BASE_DIR / 'downloads'
DOWNLOADS_DIR.mkdir(exist_ok=True)

# Store active jobs
jobs = {}


class WgetJob:
    def __init__(self, job_id, url, options, use_wget2=False, folder_name=None):
        self.id = job_id
        self.url = url
        self.options = options
        self.use_wget2 = use_wget2
        self.status = 'pending'
        self.progress = 0
        self.files_downloaded = 0
        self.total_size = '0 B'
        self.output_lines = []
        self.process = None
        self.started_at = None
        self.finished_at = None
        # Use folder_name if provided, otherwise fall back to job_id
        self.folder_name = folder_name or job_id
        self.output_dir = DOWNLOADS_DIR / self.folder_name
        self.output_dir.mkdir(exist_ok=True)
    
    def to_dict(self):
        return {
            'id': self.id,
            'url': self.url,
            'options': self.options,
            'status': self.status,
            'progress': self.progress,
            'files_downloaded': self.files_downloaded,
            'total_size': self.total_size,
            'output_lines': self.output_lines[-50:],  # Last 50 lines
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'finished_at': self.finished_at.isoformat() if self.finished_at else None,
            'output_dir': str(self.output_dir),
            'use_wget2': self.use_wget2
        }


def build_wget_command(job):
    """Build wget command from job options"""
    if job.use_wget2:
        cmd = [WGET2_PATH]
    else:
        cmd = [WGET_PATH]
    opts = job.options
    
    # Recursive options
    if opts.get('recursive', False):
        cmd.append('-r')
        depth = opts.get('depth', 2)
        cmd.extend(['-l', str(depth)])
    
    # Page requisites (CSS, JS, images)
    if opts.get('page_requisites', True):
        cmd.append('-p')
    
    # Convert links for offline viewing
    if opts.get('convert_links', True):
        cmd.append('-k')
    
    # Adjust extensions (.html)
    if opts.get('adjust_extensions', True):
        cmd.append('-E')
    
    # Span hosts (for external resources)
    if opts.get('span_hosts', False):
        cmd.append('-H')
    
    # Include subdomains - extract domain from URL and add wildcard
    if opts.get('include_subdomains', False):
        from urllib.parse import urlparse
        parsed = urlparse(job.url)
        domain = parsed.netloc
        # Remove www. prefix if present
        if domain.startswith('www.'):
            domain = domain[4:]
        # Add domain filter to include all subdomains
        cmd.extend(['-D', f'.{domain},{domain}'])
    
    # Additional domains to include
    extra_domains = opts.get('extra_domains', '')
    if extra_domains:
        cmd.extend(['-D', extra_domains])
    
    # No parent (don't go up directories)
    if opts.get('no_parent', True):
        cmd.append('--no-parent')
    
    # Rate limit
    rate_limit = opts.get('rate_limit', '')
    if rate_limit:
        cmd.extend(['--limit-rate', rate_limit])
    
    # Wait between requests
    wait = opts.get('wait', 0.5)
    if wait:
        cmd.extend(['--wait', str(wait)])
    
    # Random wait
    if opts.get('random_wait', False):
        cmd.append('--random-wait')
    
    # User agent
    user_agent = opts.get('user_agent', '')
    if user_agent:
        cmd.extend(['--user-agent', user_agent])
    
    # Reject patterns
    reject = opts.get('reject', '')
    if reject:
        cmd.extend(['--reject', reject])
    
    # Accept patterns
    accept = opts.get('accept', '')
    if accept:
        cmd.extend(['--accept', accept])
    
    # Timeout
    timeout = opts.get('timeout', 30)
    cmd.extend(['--timeout', str(timeout)])
    
    # Retries
    retries = opts.get('retries', 3)
    cmd.extend(['--tries', str(retries)])
    
    # Restrict file names (fix query strings in filenames)
    if opts.get('restrict_file_names', True):
        cmd.append('--restrict-file-names=windows')
    
    # Trust server names (use final URL after redirects)
    if opts.get('trust_server_names', True):
        cmd.append('--trust-server-names')
    
    # Ignore robots.txt
    if opts.get('ignore_robots', False):
        cmd.append('-e')
        cmd.append('robots=off')
    
    # No cookies (each page as new visit)
    if opts.get('no_cookies', False):
        cmd.append('--no-cookies')
    
    # Mirror mode (recursive + timestamps + infinite depth)
    if opts.get('mirror_mode', False):
        cmd.append('--mirror')
    
    # Output directory
    cmd.extend(['-P', str(job.output_dir)])
    
    # Progress output
    cmd.append('--progress=dot:default')
    
    # URL
    cmd.append(job.url)
    
    return cmd


def run_wget_job(job_id):
    """Run wget job in background"""
    job = jobs.get(job_id)
    if not job:
        return
    
    job.status = 'running'
    job.started_at = datetime.now()
    
    cmd = build_wget_command(job)
    job.output_lines.append(f"Command: {' '.join(cmd)}")
    
    socketio.emit('job_update', job.to_dict())
    
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        job.process = process
        
        for line in iter(process.stdout.readline, ''):
            line = line.strip()
            if line:
                job.output_lines.append(line)
                
                # Parse progress info
                if 'saved' in line.lower() or 'Saving' in line or 'saved' in line:
                    job.files_downloaded += 1
                
                # Calculate current size
                try:
                    total_size = sum(f.stat().st_size for f in job.output_dir.rglob('*') if f.is_file())
                    job.total_size = format_size(total_size)
                    job.files_downloaded = sum(1 for _ in job.output_dir.rglob('*') if _.is_file())
                except:
                    pass
                
                # Emit update every line for real-time feedback
                socketio.emit('job_update', job.to_dict())
        
        process.wait()
        
        job.status = 'completed' if process.returncode == 0 else 'failed'
        job.finished_at = datetime.now()
        
        # Calculate total size
        total_size = sum(f.stat().st_size for f in job.output_dir.rglob('*') if f.is_file())
        job.total_size = format_size(total_size)
        job.files_downloaded = sum(1 for _ in job.output_dir.rglob('*') if _.is_file())
        
    except Exception as e:
        job.status = 'failed'
        job.output_lines.append(f"Error: {str(e)}")
        job.finished_at = datetime.now()
    
    socketio.emit('job_update', job.to_dict())


def format_size(size_bytes):
    """Format bytes to human readable"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


@app.route('/')
def index():
    return render_template('index.html', downloads_dir=str(DOWNLOADS_DIR))


@app.route('/api/jobs', methods=['GET'])
def get_jobs():
    return jsonify([job.to_dict() for job in jobs.values()])


@app.route('/api/jobs', methods=['POST'])
def create_job():
    data = request.json
    url = data.get('url', '').strip()
    
    if not url:
        return jsonify({'error': 'URL is required'}), 400
    
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    
    # Generate folder name from domain
    from urllib.parse import urlparse
    parsed = urlparse(url)
    domain = parsed.netloc
    # Remove www. prefix
    if domain.startswith('www.'):
        domain = domain[4:]
    # Create unique folder name with timestamp
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    folder_name = f"{domain}_{timestamp}"
    
    job_id = str(uuid.uuid4())[:8]
    options = data.get('options', {})
    use_wget2 = data.get('use_wget2', False)
    
    job = WgetJob(job_id, url, options, use_wget2, folder_name)
    jobs[job_id] = job
    
    # Start job in background
    thread = threading.Thread(target=run_wget_job, args=(job_id,))
    thread.daemon = True
    thread.start()
    
    return jsonify(job.to_dict())


@app.route('/api/jobs/<job_id>', methods=['GET'])
def get_job(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(job.to_dict())


@app.route('/api/jobs/<job_id>', methods=['DELETE'])
def delete_job(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    
    # Kill process if running
    if job.process and job.process.poll() is None:
        job.process.terminate()
    
    # Remove output directory
    if job.output_dir.exists():
        shutil.rmtree(job.output_dir)
    
    del jobs[job_id]
    return jsonify({'success': True})


@app.route('/api/jobs/<job_id>/stop', methods=['POST'])
def stop_job(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    
    if job.process and job.process.poll() is None:
        job.process.terminate()
        job.status = 'stopped'
        job.finished_at = datetime.now()
    
    return jsonify(job.to_dict())


@app.route('/api/jobs/<job_id>/files')
def get_job_files(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    
    files = []
    for f in job.output_dir.rglob('*'):
        if f.is_file():
            rel_path = f.relative_to(job.output_dir)
            files.append({
                'path': str(rel_path),
                'size': format_size(f.stat().st_size),
                'type': f.suffix[1:] if f.suffix else 'file'
            })
    
    return jsonify(files)


@app.route('/api/jobs/<job_id>/download')
def download_job(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    
    # Create zip archive
    zip_path = DOWNLOADS_DIR / f"{job_id}.zip"
    shutil.make_archive(str(zip_path.with_suffix('')), 'zip', job.output_dir)
    
    return send_file(zip_path, as_attachment=True, download_name=f"wget-{job_id}.zip")


@app.route('/api/jobs/<job_id>/browse/<path:filepath>')
def browse_file(job_id, filepath):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    
    file_path = job.output_dir / filepath
    if not file_path.exists():
        return jsonify({'error': 'File not found'}), 404
    
    return send_from_directory(job.output_dir, filepath)


@app.route('/api/jobs/<job_id>/open')
def open_in_browser(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    
    # Find index.html
    index_files = list(job.output_dir.rglob('index.html'))
    if index_files:
        os.system(f'open "{index_files[0]}"')
        return jsonify({'success': True, 'file': str(index_files[0])})
    
    return jsonify({'error': 'No index.html found'}), 404


@app.route('/api/jobs/<job_id>/open-folder')
def open_folder(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    
    os.system(f'open "{job.output_dir}"')
    return jsonify({'success': True, 'path': str(job.output_dir)})


@app.route('/api/config')
def get_config():
    wget2_available = os.path.exists(WGET2_PATH)
    return jsonify({
        'downloads_dir': str(DOWNLOADS_DIR),
        'wget_path': WGET_PATH,
        'wget2_path': WGET2_PATH,
        'wget2_available': wget2_available
    })


@app.route('/api/open-downloads')
def open_downloads_folder():
    os.system(f'open "{DOWNLOADS_DIR}"')
    return jsonify({'success': True, 'path': str(DOWNLOADS_DIR)})


@app.route('/api/open-folder')
def open_any_folder():
    path = request.args.get('path', '')
    if path and os.path.exists(path):
        os.system(f'open "{path}"')
        return jsonify({'success': True})
    return jsonify({'error': 'Path not found'}), 404


@app.route('/api/open-site')
def open_site_in_browser():
    path = request.args.get('path', '')
    if path and os.path.exists(path):
        # Find index.html
        index_files = list(Path(path).rglob('index.html'))
        if index_files:
            os.system(f'open "{index_files[0]}"')
            return jsonify({'success': True, 'file': str(index_files[0])})
        # If no index.html, just open folder
        os.system(f'open "{path}"')
        return jsonify({'success': True, 'opened': 'folder'})
    return jsonify({'error': 'Path not found'}), 404


@app.route('/api/downloads')
def list_downloads():
    """List all downloaded folders in downloads directory"""
    downloads = []
    for item in DOWNLOADS_DIR.iterdir():
        if item.is_dir():
            # Calculate folder size and file count
            files = list(item.rglob('*'))
            file_count = sum(1 for f in files if f.is_file())
            total_size = sum(f.stat().st_size for f in files if f.is_file())
            
            # Get modification time
            mtime = datetime.fromtimestamp(item.stat().st_mtime)
            
            downloads.append({
                'name': item.name,
                'path': str(item),
                'files': file_count,
                'size': format_size(total_size),
                'date': mtime.strftime('%Y-%m-%d %H:%M')
            })
    
    # Sort by date descending
    downloads.sort(key=lambda x: x['date'], reverse=True)
    return jsonify(downloads)


@app.route('/api/browse/<path:filepath>')
def browse_download(filepath):
    """Serve files from downloads directory for built-in viewer"""
    return send_from_directory(DOWNLOADS_DIR, filepath)


@app.route('/api/downloads/<folder_name>', methods=['DELETE'])
def delete_download(folder_name):
    """Delete a downloaded folder"""
    import shutil
    folder_path = DOWNLOADS_DIR / folder_name
    if not folder_path.exists():
        return jsonify({'error': 'Folder not found'}), 404
    
    try:
        shutil.rmtree(folder_path)
        return jsonify({'success': True, 'deleted': folder_name})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/find-index/<folder_name>')
def find_index_html(folder_name):
    """Find index.html in a download folder"""
    folder_path = DOWNLOADS_DIR / folder_name
    if not folder_path.exists():
        return jsonify({'error': 'Folder not found'}), 404
    
    # Search for index.html
    index_files = list(folder_path.rglob('index.html'))
    if index_files:
        # Return the first one, relative to downloads dir
        rel_path = index_files[0].relative_to(DOWNLOADS_DIR)
        return jsonify({'path': str(rel_path)})
    
    # No index.html found, return first HTML file
    html_files = list(folder_path.rglob('*.html'))
    if html_files:
        rel_path = html_files[0].relative_to(DOWNLOADS_DIR)
        return jsonify({'path': str(rel_path)})
    
    return jsonify({'error': 'No HTML files found'}), 404


@socketio.on('connect')
def handle_connect():
    emit('connected', {'status': 'ok'})


if __name__ == '__main__':
    print(f"Wget Admin starting...")
    print(f"Using wget: {WGET_PATH}")
    print(f"Downloads dir: {DOWNLOADS_DIR}")
    socketio.run(app, host='0.0.0.0', port=5050, debug=True)
