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
import signal
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, render_template, request, jsonify, send_from_directory, send_file
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['SECRET_KEY'] = 'wget-admin-secret-key'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Configuration
BASE_DIR = Path(__file__).parent.parent
WGET_PATH = os.environ.get('WGET_PATH', str(BASE_DIR / 'src' / 'wget'))
WGET2_PATH = os.environ.get('WGET2_PATH', '/opt/homebrew/bin/wget2')
HTTRACK_PATH = os.environ.get('HTTRACK_PATH', '/opt/homebrew/bin/httrack')
PUPPETEER_SCRIPT = BASE_DIR / 'admin' / 'puppeteer-crawler.js'
DOWNLOADS_DIR = BASE_DIR / 'downloads'
DOWNLOADS_DIR.mkdir(exist_ok=True)
JOBS_FILE = BASE_DIR / 'admin' / 'jobs.json'

# Store active jobs
jobs = {}


def save_jobs():
    """Save jobs to JSON file for persistence"""
    jobs_data = {}
    for job_id, job in jobs.items():
        jobs_data[job_id] = {
            'id': job.id,
            'url': job.url,
            'options': job.options,
            'use_wget2': job.use_wget2,
            'engine': job.engine,
            'status': job.status,
            'files_downloaded': job.files_downloaded,
            'total_size': job.total_size,
            'output_lines': job.output_lines[-100:],  # Last 100 lines
            'started_at': job.started_at.isoformat() if job.started_at else None,
            'finished_at': job.finished_at.isoformat() if job.finished_at else None,
            'folder_name': job.folder_name
        }
    try:
        with open(JOBS_FILE, 'w') as f:
            json.dump(jobs_data, f, indent=2)
    except Exception as e:
        print(f"Error saving jobs: {e}")


def load_jobs():
    """Load jobs from JSON file on startup"""
    global jobs
    if not JOBS_FILE.exists():
        return
    try:
        with open(JOBS_FILE, 'r') as f:
            jobs_data = json.load(f)
        for job_id, data in jobs_data.items():
            job = WgetJob(
                data['id'],
                data['url'],
                data['options'],
                data['use_wget2'],
                data['folder_name'],
                data.get('engine', 'wget2')
            )
            job.status = data['status']
            job.files_downloaded = data['files_downloaded']
            job.total_size = data['total_size']
            job.output_lines = data['output_lines']
            if data['started_at']:
                job.started_at = datetime.fromisoformat(data['started_at'])
            if data['finished_at']:
                job.finished_at = datetime.fromisoformat(data['finished_at'])
            # Mark running jobs as stopped (server restarted)
            if job.status in ('running', 'pending', 'paused'):
                job.status = 'stopped'
                job.finished_at = datetime.now()
            jobs[job_id] = job
        print(f"Loaded {len(jobs)} jobs from file")
    except Exception as e:
        print(f"Error loading jobs: {e}")


class WgetJob:
    def __init__(self, job_id, url, options, use_wget2=False, folder_name=None, engine='wget2'):
        self.id = job_id
        self.url = url
        self.options = options
        self.use_wget2 = use_wget2
        self.engine = engine  # 'wget', 'wget2', 'puppeteer', 'httrack'
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
            'use_wget2': self.use_wget2,
            'engine': self.engine
        }


def build_wget_command(job):
    """Build wget command from job options"""
    if job.use_wget2:
        cmd = [WGET2_PATH]
        # wget2-specific optimizations (all verified options)
        cmd.append('--http2')  # Enable HTTP/2
        cmd.append('--compression=br,gzip,zstd')  # All compression types
        cmd.append('--tcp-fastopen')  # TCP Fast Open
        cmd.append('--dns-cache')  # DNS caching
        cmd.append('--hsts')  # HTTP Strict Transport Security
        
        # Progress bar style
        if job.options.get('progress_bar', True):
            cmd.append('--progress=bar')
        
        # Metalink support for mirrors
        if job.options.get('metalink', False):
            cmd.append('--metalink')
        
        # Parallel threads (default 5, max 20)
        threads = job.options.get('parallel_threads', 10)
        cmd.extend(['--max-threads', str(threads)])
        
        # HTTP/2 request window for parallel requests
        http2_window = job.options.get('http2_window', 30)
        cmd.extend(['--http2-request-window', str(http2_window)])
        
        # Chunk size for parallel download of single files
        chunk_size = job.options.get('chunk_size', '')
        if chunk_size:
            cmd.extend(['--chunk-size', chunk_size])
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
    
    # Collect all domains for -D option
    domains_list = []
    
    # Include subdomains - extract domain from URL and add wildcard
    if opts.get('include_subdomains', False):
        from urllib.parse import urlparse
        parsed = urlparse(job.url)
        domain = parsed.netloc
        # Remove www. prefix if present
        if domain.startswith('www.'):
            domain = domain[4:]
        # Add base domain and wildcard for subdomains
        domains_list.append(domain)
        domains_list.append(f'.{domain}')
        # Also add www variant
        domains_list.append(f'www.{domain}')
    
    # Additional domains to include
    extra_domains = opts.get('extra_domains', '')
    if extra_domains:
        # Split by comma and add each domain
        for d in extra_domains.split(','):
            d = d.strip()
            if d:
                domains_list.append(d)
    
    # Apply -D option with all collected domains
    if domains_list:
        cmd.extend(['-D', ','.join(domains_list)])
    
    # Span hosts - required for subdomains to work, auto-enable if subdomains requested
    if opts.get('span_hosts', False) or opts.get('include_subdomains', False):
        cmd.append('-H')
    
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
        if job.use_wget2:
            cmd.append('--robots=off')
        else:
            cmd.append('-e')
            cmd.append('robots=off')
    
    # No cookies (each page as new visit)
    if opts.get('no_cookies', False):
        cmd.append('--no-cookies')
    
    # Mirror mode (recursive + timestamps + infinite depth)
    if opts.get('mirror_mode', False):
        cmd.append('--mirror')
    
    # Continue/resume download (timestamping + no-clobber)
    if opts.get('continue_download', False):
        cmd.append('--timestamping')
        cmd.append('--no-clobber')
    
    # Always add no-clobber to prevent re-downloading existing files
    if not opts.get('continue_download', False) and not opts.get('mirror_mode', False):
        cmd.append('--no-clobber')
    
    # Output directory
    cmd.extend(['-P', str(job.output_dir)])
    
    # Progress output
    cmd.append('--progress=dot:default')
    
    # URL
    cmd.append(job.url)
    
    return cmd


def build_httrack_command(job):
    """Build httrack command from job options"""
    cmd = [HTTRACK_PATH]
    opts = job.options
    
    # URL first
    cmd.append(job.url)
    
    # Output directory
    cmd.extend(['-O', str(job.output_dir)])
    
    # Depth
    depth = opts.get('depth', 2)
    cmd.extend(['-r' + str(depth)])
    
    # Include subdomains
    if opts.get('include_subdomains', False):
        cmd.append('-%e0')  # Follow external links on same domain
    
    # User agent
    user_agent = opts.get('user_agent', '')
    if user_agent:
        cmd.extend(['-F', user_agent])
    
    # Rate limit
    rate_limit = opts.get('rate_limit', '')
    if rate_limit:
        # Convert to bytes/sec for httrack
        cmd.extend(['-%c', '1'])  # Max connections
    
    # Timeout
    timeout = opts.get('timeout', 30)
    cmd.extend(['-T', str(timeout)])
    
    # Retries
    retries = opts.get('retries', 3)
    cmd.extend(['-R', str(retries)])
    
    # Continue download
    if opts.get('continue_download', False):
        cmd.append('--continue')
    
    # Verbose output
    cmd.append('-v')
    
    return cmd


def build_puppeteer_command(job):
    """Build puppeteer crawler command from job options"""
    cmd = ['node', str(PUPPETEER_SCRIPT)]
    opts = job.options
    
    # URL
    cmd.append(job.url)
    
    # Output directory
    cmd.append(str(job.output_dir))
    
    # Max pages
    max_pages = opts.get('max_pages', 100)
    cmd.append(str(max_pages))
    
    # Scroll for infinite scroll pages
    if opts.get('js_scroll', True):
        cmd.append('--scroll')
    
    # Click "Show More" buttons
    if opts.get('js_click_more', True):
        cmd.append('--click-more')
    
    # Wait time for JS rendering
    wait_time = opts.get('js_wait', 2000)
    cmd.append(f'--wait={wait_time}')
    
    # Depth
    depth = opts.get('depth', 3)
    cmd.append(f'--depth={depth}')
    
    return cmd


def run_single_engine(job, engine_name, cmd):
    """Run a single engine and return success status"""
    job.output_lines.append(f"")
    job.output_lines.append(f"{'='*50}")
    job.output_lines.append(f"Running: {engine_name}")
    job.output_lines.append(f"Command: {' '.join(cmd)}")
    job.output_lines.append(f"{'='*50}")
    
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
                
                update_job_stats(job)
                socketio.emit('job_update', job.to_dict())
        
        process.wait()
        return process.returncode == 0
    except Exception as e:
        job.output_lines.append(f"Error in {engine_name}: {str(e)}")
        return False


def run_wget_job(job_id):
    """Run download job in background (supports multiple engines)"""
    job = jobs.get(job_id)
    if not job:
        return
    
    job.status = 'running'
    job.started_at = datetime.now()
    
    # Smart mode - run all engines sequentially
    if job.engine == 'smart':
        job.output_lines.append("SMART MODE: Running all engines for maximum coverage")
        socketio.emit('job_update', job.to_dict())
        save_jobs()
        
        # Step 1: wget2 for fast static content
        cmd_wget2 = build_wget_command(job)
        run_single_engine(job, "wget2 (static content)", cmd_wget2)
        
        # Step 2: Puppeteer for JS-rendered content
        cmd_puppeteer = build_puppeteer_command(job)
        run_single_engine(job, "Puppeteer (JS rendering)", cmd_puppeteer)
        
        # Step 3: httrack for anything missed
        cmd_httrack = build_httrack_command(job)
        run_single_engine(job, "HTTrack (final pass)", cmd_httrack)
        
        # Finish
        job.status = 'completed'
        job.finished_at = datetime.now()
        job.output_lines.append("")
        job.output_lines.append("SMART MODE COMPLETED - All engines finished")
        socketio.emit('job_update', job.to_dict())
        save_jobs()
        return
    
    # Single engine mode
    if job.engine == 'puppeteer':
        cmd = build_puppeteer_command(job)
    elif job.engine == 'httrack':
        cmd = build_httrack_command(job)
    else:
        cmd = build_wget_command(job)
    
    job.output_lines.append(f"Engine: {job.engine}")
    job.output_lines.append(f"Command: {' '.join(cmd)}")
    
    socketio.emit('job_update', job.to_dict())
    save_jobs()
    
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
                
                update_job_stats(job)
                socketio.emit('job_update', job.to_dict())
        
        process.wait()
        
        job.status = 'completed' if process.returncode == 0 else 'failed'
        job.finished_at = datetime.now()
        update_job_stats(job)
        
    except Exception as e:
        job.status = 'failed'
        job.output_lines.append(f"Error: {str(e)}")
        job.finished_at = datetime.now()
    
    socketio.emit('job_update', job.to_dict())
    save_jobs()


def format_size(size_bytes):
    """Format bytes to human readable"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def update_job_stats(job):
    """Update job file count and total size"""
    try:
        files = list(job.output_dir.rglob('*'))
        job.files_downloaded = sum(1 for f in files if f.is_file())
        total_size = sum(f.stat().st_size for f in files if f.is_file())
        job.total_size = format_size(total_size)
    except:
        pass


def start_job_thread(job_id):
    """Start job in background thread"""
    thread = threading.Thread(target=run_wget_job, args=(job_id,))
    thread.daemon = True
    thread.start()


def find_index_file(folder_path):
    """Find index.html or first HTML file in folder"""
    index_files = list(folder_path.rglob('index.html'))
    if index_files:
        return index_files[0]
    html_files = list(folder_path.rglob('*.html'))
    if html_files:
        return html_files[0]
    return None


def extract_domain_from_url(url):
    """Extract clean domain from URL"""
    parsed = urlparse(url)
    domain = parsed.netloc
    if domain.startswith('www.'):
        domain = domain[4:]
    return domain


def normalize_url(url):
    """Normalize URL for duplicate comparison"""
    parsed = urlparse(url)
    # Remove www., trailing slash, default ports
    domain = parsed.netloc.lower()
    if domain.startswith('www.'):
        domain = domain[4:]
    # Remove default ports
    domain = domain.replace(':80', '').replace(':443', '')
    # Normalize path
    path = parsed.path.rstrip('/') or '/'
    return f"{parsed.scheme}://{domain}{path}"


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
    
    # Normalize URL for duplicate check
    normalized_url = normalize_url(url)
    
    # Check for duplicate: same URL already running or pending
    for existing_job in jobs.values():
        if existing_job.status in ('running', 'pending', 'paused'):
            if normalize_url(existing_job.url) == normalized_url:
                return jsonify({
                    'error': 'This URL is already being downloaded',
                    'existing_job_id': existing_job.id
                }), 409
    
    # Generate folder name from domain
    domain = extract_domain_from_url(url)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    folder_name = f"{domain}_{timestamp}"
    
    job_id = str(uuid.uuid4())[:8]
    options = data.get('options', {})
    use_wget2 = data.get('use_wget2', False)
    engine = data.get('engine', 'wget2' if use_wget2 else 'wget')
    
    job = WgetJob(job_id, url, options, use_wget2, folder_name, engine)
    jobs[job_id] = job
    save_jobs()
    
    start_job_thread(job_id)
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
    save_jobs()
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
        save_jobs()
    
    return jsonify(job.to_dict())


@app.route('/api/jobs/<job_id>/pause', methods=['POST'])
def pause_job(job_id):
    """Pause a running job using SIGSTOP"""
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    
    if job.process and job.process.poll() is None and job.status == 'running':
        job.process.send_signal(signal.SIGSTOP)
        job.status = 'paused'
        socketio.emit('job_update', job.to_dict())
        save_jobs()
    
    return jsonify(job.to_dict())


@app.route('/api/jobs/<job_id>/resume', methods=['POST'])
def resume_job(job_id):
    """Resume a paused job using SIGCONT"""
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    
    if job.process and job.process.poll() is None and job.status == 'paused':
        job.process.send_signal(signal.SIGCONT)
        job.status = 'running'
        socketio.emit('job_update', job.to_dict())
        save_jobs()
    
    return jsonify(job.to_dict())


@app.route('/api/jobs/<job_id>/restart', methods=['POST'])
def restart_job(job_id):
    """Restart a job with same URL and options"""
    old_job = jobs.get(job_id)
    if not old_job:
        return jsonify({'error': 'Job not found'}), 404
    
    # Stop if still running
    if old_job.process and old_job.process.poll() is None:
        old_job.process.terminate()
    
    # Get engine from request or use old job's engine
    data = request.json or {}
    engine = data.get('engine', old_job.engine)
    use_wget2 = data.get('use_wget2', old_job.use_wget2)
    
    # Create new job with selected engine
    new_job_id = str(uuid.uuid4())[:8]
    new_job = WgetJob(new_job_id, old_job.url, old_job.options.copy(), use_wget2, old_job.folder_name, engine)
    new_job.options['continue_download'] = True  # Resume mode
    jobs[new_job_id] = new_job
    
    # Remove old job
    del jobs[job_id]
    save_jobs()
    
    start_job_thread(new_job_id)
    return jsonify(new_job.to_dict())


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
    
    index_file = find_index_file(job.output_dir)
    if index_file:
        os.system(f'open "{index_file}"')
        return jsonify({'success': True, 'file': str(index_file)})
    
    return jsonify({'error': 'No HTML files found'}), 404


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
        index_file = find_index_file(Path(path))
        if index_file:
            os.system(f'open "{index_file}"')
            return jsonify({'success': True, 'file': str(index_file)})
        # If no HTML files, just open folder
        os.system(f'open "{path}"')
        return jsonify({'success': True, 'opened': 'folder'})
    return jsonify({'error': 'Path not found'}), 404


@app.route('/api/downloads')
def list_downloads():
    """List all downloaded folders in downloads directory"""
    # Get list of folders currently being downloaded
    active_folders = set()
    for job in jobs.values():
        if job.status in ('running', 'pending', 'paused'):
            active_folders.add(job.folder_name)
    
    downloads = []
    for item in DOWNLOADS_DIR.iterdir():
        if item.is_dir():
            # Calculate folder size and file count
            files = list(item.rglob('*'))
            file_count = sum(1 for f in files if f.is_file())
            total_size = sum(f.stat().st_size for f in files if f.is_file())
            
            # Get modification time
            mtime = datetime.fromtimestamp(item.stat().st_mtime)
            
            # Check if currently downloading
            is_active = item.name in active_folders
            status = 'downloading' if is_active else 'completed'
            
            downloads.append({
                'name': item.name,
                'path': str(item),
                'files': file_count,
                'size': format_size(total_size),
                'date': mtime.strftime('%Y-%m-%d %H:%M'),
                'status': status,
                'is_active': is_active
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
    folder_path = DOWNLOADS_DIR / folder_name
    if not folder_path.exists():
        return jsonify({'error': 'Folder not found'}), 404
    
    try:
        shutil.rmtree(folder_path)
        return jsonify({'success': True, 'deleted': folder_name})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/downloads/<folder_name>/continue', methods=['POST'])
def continue_download(folder_name):
    """Continue downloading an existing folder (resume incomplete download)"""
    folder_path = DOWNLOADS_DIR / folder_name
    if not folder_path.exists():
        return jsonify({'error': 'Folder not found'}), 404
    
    data = request.json or {}
    
    # Extract domain from folder name (format: domain_timestamp)
    parts = folder_name.split('_')
    domain = '_'.join(parts[:-2]) if len(parts) > 2 else parts[0]
    url = data.get('url', f'https://{domain}')
    
    # Default options for continue (with timestamping for resume)
    options = data.get('options', {
        'recursive': True,
        'depth': 5,
        'page_requisites': True,
        'convert_links': True,
        'no_parent': True,
        'include_subdomains': True,
        'ignore_robots': True
    })
    
    use_wget2 = data.get('use_wget2', False)
    
    engine = data.get('engine', 'smart')
    job_id = str(uuid.uuid4())[:8]
    job = WgetJob(job_id, url, options, use_wget2, folder_name, engine)
    job.options['continue_download'] = True
    jobs[job_id] = job
    
    start_job_thread(job_id)
    return jsonify(job.to_dict())


@app.route('/api/downloads/<folder_name>/restart', methods=['POST'])
def restart_download(folder_name):
    """Restart download from scratch (delete and re-download)"""
    folder_path = DOWNLOADS_DIR / folder_name
    if not folder_path.exists():
        return jsonify({'error': 'Folder not found'}), 404
    
    data = request.json or {}
    
    # Extract domain from folder name
    parts = folder_name.split('_')
    domain = '_'.join(parts[:-2]) if len(parts) > 2 else parts[0]
    url = data.get('url', f'https://{domain}')
    
    # Delete existing folder
    shutil.rmtree(folder_path)
    
    # Default options for full re-download
    options = data.get('options', {
        'recursive': True,
        'depth': 5,
        'page_requisites': True,
        'convert_links': True,
        'no_parent': True,
        'include_subdomains': True,
        'ignore_robots': True,
        'mirror_mode': True
    })
    
    use_wget2 = data.get('use_wget2', True)
    engine = data.get('engine', 'smart')
    
    job_id = str(uuid.uuid4())[:8]
    job = WgetJob(job_id, url, options, use_wget2, folder_name, engine)
    jobs[job_id] = job
    
    start_job_thread(job_id)
    return jsonify(job.to_dict())


@app.route('/api/jobs/active')
def get_active_jobs():
    """Get only running jobs for active downloads display"""
    active = [job.to_dict() for job in jobs.values() if job.status in ('pending', 'running')]
    return jsonify(active)


@app.route('/api/find-index/<folder_name>')
def find_index_html_api(folder_name):
    """Find index.html in a download folder"""
    folder_path = DOWNLOADS_DIR / folder_name
    if not folder_path.exists():
        return jsonify({'error': 'Folder not found'}), 404
    
    index_file = find_index_file(folder_path)
    if index_file:
        rel_path = index_file.relative_to(DOWNLOADS_DIR)
        return jsonify({'path': str(rel_path)})
    
    return jsonify({'error': 'No HTML files found'}), 404


@socketio.on('connect')
def handle_connect():
    emit('connected', {'status': 'ok'})


if __name__ == '__main__':
    print(f"Wget Admin starting...")
    print(f"Using wget: {WGET_PATH}")
    print(f"Downloads dir: {DOWNLOADS_DIR}")
    load_jobs()  # Load saved jobs on startup
    socketio.run(app, host='0.0.0.0', port=5050, debug=True)
