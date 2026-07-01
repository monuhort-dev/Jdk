import os
import shutil
import zipfile
import subprocess
import threading
import time
from datetime import datetime

from functools import wraps
from pathlib import Path

from flask import (Flask, render_template, request, redirect,
                   url_for, flash, abort, send_from_directory,
                   Response, stream_with_context, jsonify)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, login_user,
                         logout_user, login_required, current_user)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SESSION_SECRET', 'dev-secret-change-me')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///bothost.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100 MB hard cap
app.config['REMEMBER_COOKIE_DURATION'] = 30 * 24 * 60 * 60  # 30 days in seconds
app.config['REMEMBER_COOKIE_HTTPONLY'] = True
app.config['REMEMBER_COOKIE_SAMESITE'] = 'Lax'

UPLOAD_BASE     = Path(__file__).parent / 'uploads'
UPLOAD_BASE.mkdir(exist_ok=True)
APP_DIR         = Path(__file__).parent
PYTHON_PREAMBLE = APP_DIR / '_sandbox_preamble.py'
NODE_PREAMBLE   = APP_DIR / '_sandbox_preamble.js'

running_processes = {}


def _run_process(server_id, cmd, cwd, log_path, env):
    with app.app_context():
        try:
            with open(log_path, 'a') as lf:
                proc = subprocess.Popen(
                    cmd, cwd=str(cwd),
                    stdout=lf, stderr=lf,
                    env=env, text=True,
                )
            running_processes[server_id] = proc
            proc.wait()
        except Exception as exc:
            with open(log_path, 'a') as lf:
                lf.write(f'[{datetime.utcnow():%Y-%m-%d %H:%M:%S}] [ERROR] Failed to start process: {exc}\n')
        finally:
            running_processes.pop(server_id, None)
            s = db.session.get(Server, server_id)
            if s and s.status == 'running':
                s.status = 'stopped'
                db.session.commit()
                ts = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
                with open(UPLOAD_BASE / str(server_id) / 'bothost.log', 'a') as lf:
                    lf.write(f'[{ts}] [STOP] Process exited\n')


def _pip_install_req(req_file, cwd, log_path):
    """Install packages from a requirements.txt into .packages/."""
    pkg_dir = cwd / '.packages'
    pkg_dir.mkdir(exist_ok=True)
    uv  = shutil.which('uv')
    pip = shutil.which('pip3') or shutil.which('pip') or 'pip3'
    cmd = ([uv, 'pip', 'install', '--target', str(pkg_dir), '-r', str(req_file)]
           if uv else [pip, 'install', '--target', str(pkg_dir), '-r', str(req_file)])
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd))
    ts = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    with open(log_path, 'a') as lf:
        if result.returncode == 0:
            lf.write(f'[{ts}] [INFO] requirements.txt installed OK\n')
        else:
            err = (result.stderr or result.stdout or '').strip()[:800]
            lf.write(f'[{ts}] [ERROR] requirements.txt install failed:\n{err}\n')
    return result.returncode == 0


def _pip_install_packages(packages, cwd, log_path):
    """Install an explicit list of packages into .packages/ (from console pip install)."""
    pkg_dir = cwd / '.packages'
    pkg_dir.mkdir(exist_ok=True)
    uv  = shutil.which('uv')
    pip = shutil.which('pip3') or shutil.which('pip') or 'pip3'
    cmd = ([uv, 'pip', 'install', '--target', str(pkg_dir)] + packages
           if uv else [pip, 'install', '--target', str(pkg_dir)] + packages)
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd))
    ts = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    with open(log_path, 'a') as lf:
        if result.returncode == 0:
            lf.write(f'[{ts}] [INFO] pip install OK: {" ".join(packages)}\n')
        else:
            err = (result.stderr or result.stdout or '').strip()[:800]
            lf.write(f'[{ts}] [ERROR] pip install failed:\n{err}\n')
    return result.returncode == 0


def _install_from_requirements(server, cwd, log_path):
    """Install from requirements.txt if it exists. Called at bot startup."""
    req_file = cwd / 'requirements.txt'
    if not req_file.exists():
        return
    try:
        lines = req_file.read_text(errors='replace').splitlines()
        reqs  = [l.strip() for l in lines if l.strip() and not l.strip().startswith('#')]
        if not reqs:
            return
        ts = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        with open(log_path, 'a') as lf:
            lf.write(f'[{ts}] [INFO] Found requirements.txt ({len(reqs)} package(s)) - installing\n')
        _pip_install_req(req_file, cwd, log_path)
    except Exception as e:
        ts = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        with open(log_path, 'a') as lf:
            lf.write(f'[{ts}] [ERROR] Could not read requirements.txt: {e}\n')


def _auto_install_node(server, cwd, log_path):
    pkg_json = cwd / 'package.json'
    if not pkg_json.exists():
        return
    ts = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    with open(log_path, 'a') as lf:
        lf.write(f'[{ts}] [INFO] Running npm install\n')
    npm    = shutil.which('npm') or 'npm'
    result = subprocess.run([npm, 'install'], capture_output=True, text=True, cwd=str(cwd))
    ts = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    with open(log_path, 'a') as lf:
        if result.returncode == 0:
            lf.write(f'[{ts}] [INFO] npm install done\n')
        else:
            lf.write(f'[{ts}] [ERROR] npm install failed: {result.stderr[:400]}\n')


def start_server_process(server):
    if server.id in running_processes:
        return
    entry = server.entry_file or ('main.py' if server.bot_type == 'python' else 'index.js')
    cwd   = server.upload_dir
    env   = os.environ.copy()

    env['BOTHOST_SANDBOX_DIR']  = str(cwd.resolve())
    env['BOTHOST_UPLOADS_BASE'] = str(UPLOAD_BASE.resolve())
    env['BOTHOST_APP_DIR']      = str(APP_DIR.resolve())
    env['BOTHOST_ENTRY_FILE']   = str((cwd / entry).resolve())

    if server.bot_type == 'python':
        python = shutil.which('python3') or 'python3'
        cmd    = [python, str(PYTHON_PREAMBLE)]
    else:
        node = shutil.which('node') or 'node'
        cmd  = [node, '--require', str(NODE_PREAMBLE), entry]

    if server.env_vars:
        for line in server.env_vars.splitlines():
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, _, v = line.partition('=')
                env[k.strip()] = v.strip()

    log_path    = server.log_path
    pkg_dir     = str(cwd / '.packages')
    existing_py = env.get('PYTHONPATH', '')
    env['PYTHONPATH'] = f"{pkg_dir}:{existing_py}" if existing_py else pkg_dir

    server.append_log('START', f'Launching (sandboxed): {" ".join(cmd)}')
    if server.bot_type == 'python':
        _install_from_requirements(server, cwd, log_path)

    t = threading.Thread(
        target=_run_process,
        args=(server.id, cmd, cwd, log_path, env),
        daemon=True,
    )
    t.start()


def _proc_resources(pid):
    """Return cpu% and RSS MB for a pid using Linux /proc. No psutil needed."""
    try:
        # --- RSS memory from /proc/<pid>/status ---
        status_text = Path(f'/proc/{pid}/status').read_text()
        rss_kb = 0
        for line in status_text.splitlines():
            if line.startswith('VmRSS:'):
                rss_kb = int(line.split()[1])
                break

        # --- CPU% via two-point /proc/<pid>/stat sampling ---
        def _read_ticks(p):
            stat  = Path(f'/proc/{p}/stat').read_text().split()
            proc  = int(stat[13]) + int(stat[14])           # utime + stime
            total = sum(int(x) for x in
                        Path('/proc/stat').read_text().splitlines()[0].split()[1:])
            return proc, total

        t1_proc, t1_total = _read_ticks(pid)
        time.sleep(0.15)
        t2_proc, t2_total = _read_ticks(pid)

        delta_proc  = t2_proc  - t1_proc
        delta_total = t2_total - t1_total
        cpu_pct = round(100.0 * delta_proc / delta_total, 1) if delta_total > 0 else 0.0

        return {'cpu': cpu_pct, 'ram_mb': round(rss_kb / 1024, 1), 'pid': pid}
    except Exception:
        return None


def stop_server_process(server):
    proc = running_processes.pop(server.id, None)
    if proc:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


FREE_CPU_LIMIT  = 10
FREE_RAM_LIMIT  = 256
FREE_DISK_LIMIT = 512
FREE_FILE_SIZE  = 10

EDITABLE_EXT = {
    '.py', '.js', '.ts', '.json', '.env', '.txt', '.md',
    '.yaml', '.yml', '.toml', '.sh', '.cfg', '.ini', '.html',
    '.css', '.xml', '.csv', '.log', '.conf',
}

db            = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view         = 'login'
login_manager.login_message      = 'Please sign in to continue.'
login_manager.login_message_category = 'info'


# ── Models ────────────────────────────────────────────────────────────────────

class User(UserMixin, db.Model):
    __tablename__ = 'bh_users'
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(32), unique=True, nullable=False)
    email         = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin      = db.Column(db.Boolean, default=False)
    cpu_limit     = db.Column(db.Integer, default=FREE_CPU_LIMIT)
    ram_limit     = db.Column(db.Integer, default=FREE_RAM_LIMIT)
    disk_limit    = db.Column(db.Integer, default=FREE_DISK_LIMIT)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    servers       = db.relationship('Server', backref='owner', lazy=True,
                                    cascade='all, delete-orphan')

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

    @property
    def effective_limits(self):
        if self.is_admin:
            return {'cpu': 'inf', 'ram': 'inf', 'disk': 'inf', 'file': 'inf'}
        return {
            'cpu':  f'{self.cpu_limit}%',
            'ram':  f'{self.ram_limit} MB',
            'disk': f'{self.disk_limit} MB',
            'file': f'{FREE_FILE_SIZE} MB',
        }


class Server(db.Model):
    __tablename__ = 'bh_servers'
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('bh_users.id'), nullable=False)
    name        = db.Column(db.String(64), nullable=False)
    bot_type    = db.Column(db.String(16), default='python')
    status      = db.Column(db.String(16), default='stopped')
    entry_file  = db.Column(db.String(128))
    description = db.Column(db.String(256))
    env_vars    = db.Column(db.Text)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def upload_dir(self):
        d = UPLOAD_BASE / str(self.id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def disk_used_mb(self):
        d = UPLOAD_BASE / str(self.id)
        if not d.exists():
            return 0
        total = sum(f.stat().st_size for f in d.rglob('*') if f.is_file())
        return round(total / (1024 * 1024), 2)

    @property
    def log_path(self):
        self.upload_dir
        return UPLOAD_BASE / str(self.id) / 'bothost.log'

    def append_log(self, event, message):
        ts = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        with open(self.log_path, 'a') as f:
            f.write(f'[{ts}] [{event}] {message}\n')

    def read_log(self, lines=300):
        if not self.log_path.exists():
            return []
        with open(self.log_path, 'r') as f:
            all_lines = f.readlines()
        return [l.rstrip() for l in all_lines[-lines:]]


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ── Helpers ───────────────────────────────────────────────────────────────────

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


def owns_server(server):
    return server.user_id == current_user.id or current_user.is_admin


def safe_path(server, filename):
    """Resolve filename inside server's upload dir, reject path traversal."""
    base   = server.upload_dir.resolve()
    target = (base / filename).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        abort(400)
    return target


def safe_subpath(server, subpath):
    """Validate a directory subpath stays inside the server's upload dir."""
    base = server.upload_dir.resolve()
    if not subpath:
        return base
    target = (base / subpath).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        abort(400)
    if not target.is_dir():
        abort(404)
    return target


def dir_listing(server, subpath=''):
    """Return sorted file-info list for upload_dir / subpath."""
    base        = server.upload_dir
    listing_dir = (base / subpath) if subpath else base
    files = []
    for p in sorted(listing_dir.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        # skip internal log at root level
        if p.name == 'bothost.log' and not subpath:
            continue
        size_bytes = p.stat().st_size if p.is_file() else 0
        rel_path   = str(p.relative_to(base))
        files.append({
            'name':     p.name,
            'rel_path': rel_path,
            'is_file':  p.is_file(),
            'size':     _fmt_size(size_bytes),
            'size_raw': size_bytes,
            'editable': p.suffix.lower() in EDITABLE_EXT,
            'is_zip':   p.suffix.lower() == '.zip',
            'mtime':    datetime.fromtimestamp(p.stat().st_mtime).strftime('%Y-%m-%d %H:%M'),
        })
    return files


def _fmt_size(b):
    if b < 1024:    return f'{b} B'
    if b < 1024**2: return f'{b/1024:.1f} KB'
    return               f'{b/1024**2:.1f} MB'


def auto_create_server(user):
    server = Server(
        user_id=user.id,
        name=f'{user.username}-bot',
        bot_type='python',
        status='stopped',
        entry_file='main.py',
        description='My first bot server',
    )
    db.session.add(server)
    db.session.commit()
    return server


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        if not username or not email or not password:
            flash('All fields are required.', 'error')
            return render_template('register.html')
        if len(username) < 3:
            flash('Username must be at least 3 characters.', 'error')
            return render_template('register.html')
        if len(password) < 6:
            flash('Password must be at least 6 characters.', 'error')
            return render_template('register.html')
        if User.query.filter_by(username=username).first():
            flash('Username already taken.', 'error')
            return render_template('register.html')
        if User.query.filter_by(email=email).first():
            flash('Email already registered.', 'error')
            return render_template('register.html')
        is_first = User.query.count() == 0
        user = User(username=username, email=email, is_admin=is_first)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        auto_create_server(user)
        login_user(user)
        flash(f'Welcome, {username}! Your free server slot is ready.', 'success')
        return redirect(url_for('dashboard'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user     = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            remember = request.form.get('remember') == '1'
            login_user(user, remember=remember)
            return redirect(request.args.get('next') or url_for('dashboard'))
        flash('Invalid email or password.', 'error')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))


# ── Dashboard / Servers ───────────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    servers = Server.query.filter_by(user_id=current_user.id).all()
    return render_template('dashboard.html', servers=servers)


@app.route('/servers')
@login_required
def servers():
    return render_template('servers.html',
                           servers=Server.query.filter_by(user_id=current_user.id).all())


@app.route('/server/<int:server_id>/config', methods=['GET', 'POST'])
@login_required
def server_config(server_id):
    server = db.session.get(Server, server_id) or abort(404)
    if not owns_server(server):
        abort(403)
    if request.method == 'POST':
        server.name        = request.form.get('name', server.name).strip()[:64]
        server.bot_type    = request.form.get('bot_type', 'python')
        server.entry_file  = request.form.get('entry_file', '').strip()[:128] or None
        server.description = request.form.get('description', '').strip()[:256] or None
        server.env_vars    = request.form.get('env_vars', '').strip() or None
        db.session.commit()
        server.append_log('CONFIG', f'Configuration updated by {current_user.username}')
        flash('Server configuration saved.', 'success')
        return redirect(url_for('server_config', server_id=server.id))
    return render_template('server_config.html', server=server)


@app.route('/server/<int:server_id>/toggle', methods=['POST'])
@login_required
def server_toggle(server_id):
    server = db.session.get(Server, server_id) or abort(404)
    if not owns_server(server):
        abort(403)
    if server.status == 'stopped':
        server.status = 'running'
        db.session.commit()
        start_server_process(server)
        flash(f'{server.name} started.', 'success')
    else:
        stop_server_process(server)
        server.status = 'stopped'
        db.session.commit()
        server.append_log('STOP', f'Stopped by {current_user.username}')
        flash(f'{server.name} stopped.', 'success')
    next_url = request.form.get('next') or request.referrer or url_for('dashboard')
    return redirect(next_url)


@app.route('/server/<int:server_id>/delete', methods=['POST'])
@login_required
def server_delete(server_id):
    server = db.session.get(Server, server_id) or abort(404)
    if not owns_server(server):
        abort(403)
    ud = UPLOAD_BASE / str(server.id)
    if ud.exists():
        shutil.rmtree(ud)
    db.session.delete(server)
    db.session.commit()
    flash('Server deleted.', 'success')
    return redirect(url_for('dashboard'))


# ── Server workspace ──────────────────────────────────────────────────────────

@app.route('/server/<int:server_id>')
@login_required
def server_detail(server_id):
    return redirect(url_for('server_console', server_id=server_id))


@app.route('/server/<int:server_id>/console')
@login_required
def server_console(server_id):
    server = db.session.get(Server, server_id) or abort(404)
    if not owns_server(server):
        abort(403)
    return render_template('console.html', server=server)


@app.route('/server/<int:server_id>/stream')
@login_required
def server_stream(server_id):
    server   = db.session.get(Server, server_id) or abort(404)
    if not owns_server(server):
        abort(403)
    log_path = server.log_path

    def generate():
        if log_path.exists():
            with open(log_path, 'r', errors='replace') as f:
                for line in f:
                    line = line.rstrip()
                    if line:
                        yield f'data: {line}\n\n'
        pos = log_path.stat().st_size if log_path.exists() else 0
        while True:
            time.sleep(0.4)
            try:
                if log_path.exists():
                    size = log_path.stat().st_size
                    if size > pos:
                        with open(log_path, 'r', errors='replace') as f:
                            f.seek(pos)
                            chunk = f.read()
                        pos = size
                        for line in chunk.splitlines():
                            if line:
                                yield f'data: {line}\n\n'
                else:
                    pos = 0
            except Exception:
                pass
            yield ': ping\n\n'

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no', 'Connection': 'keep-alive'},
    )


@app.route('/server/<int:server_id>/resources')
@login_required
def server_resources(server_id):
    server = db.session.get(Server, server_id) or abort(404)
    if not owns_server(server):
        abort(403)
    proc = running_processes.get(server_id)
    if not proc or proc.poll() is not None:
        return jsonify({'status': 'stopped'})
    data = _proc_resources(proc.pid)
    if data is None:
        return jsonify({'status': 'stopped'})
    data['status'] = 'running'
    return jsonify(data)


@app.route('/server/<int:server_id>/stdin', methods=['POST'])
@login_required
def server_stdin(server_id):
    server = db.session.get(Server, server_id) or abort(404)
    if not owns_server(server):
        abort(403)
    cmd = request.form.get('cmd', '').strip()[:512]
    if not cmd:
        return ('', 204)

    server.append_log('STDIN', f'$ {cmd}')

    # Intercept pip install / pip3 install and run them for real
    import shlex
    tokens = shlex.split(cmd)
    is_pip = (len(tokens) >= 3
              and tokens[0] in ('pip', 'pip3', 'python3 -m pip')
              and tokens[1] == 'install')
    # also handle "python3 -m pip install ..."
    if not is_pip and len(tokens) >= 4 and tokens[:3] == ['python3', '-m', 'pip'] and tokens[3] == 'install':
        is_pip = True
        tokens = ['pip', 'install'] + tokens[4:]

    if is_pip:
        packages = [t for t in tokens[2:] if not t.startswith('-')]
        if packages:
            cwd      = server.upload_dir
            log_path = server.log_path
            def _run_pip():
                with app.app_context():
                    _pip_install_packages(packages, cwd, log_path)
            threading.Thread(target=_run_pip, daemon=True).start()
        return ('', 204)

    # Not a pip command — pass to the bot's stdin if it's running
    proc = running_processes.get(server_id)
    if proc and proc.stdin:
        try:
            proc.stdin.write(cmd + '\n')
            proc.stdin.flush()
        except Exception:
            server.append_log('ERROR', f'Could not write to stdin')
    return ('', 204)


# ── Log viewer ────────────────────────────────────────────────────────────────

@app.route('/server/<int:server_id>/logs')
@login_required
def server_logs(server_id):
    server = db.session.get(Server, server_id) or abort(404)
    if not owns_server(server):
        abort(403)
    return render_template('logs.html', server=server, log_lines=server.read_log())


@app.route('/server/<int:server_id>/logs/clear', methods=['POST'])
@login_required
def log_clear(server_id):
    server = db.session.get(Server, server_id) or abort(404)
    if not owns_server(server):
        abort(403)
    if server.log_path.exists():
        server.log_path.unlink()
    server.append_log('INFO', f'Log cleared by {current_user.username}')
    flash('Logs cleared.', 'success')
    return redirect(url_for('server_logs', server_id=server_id))


@app.route('/server/<int:server_id>/logs/inject', methods=['POST'])
@login_required
@admin_required
def log_inject(server_id):
    server  = db.session.get(Server, server_id) or abort(404)
    event   = request.form.get('event', 'INFO').upper()[:16]
    message = request.form.get('message', '').strip()[:512]
    if message:
        server.append_log(event, message)
        flash('Log entry injected.', 'success')
    return redirect(url_for('server_logs', server_id=server_id))


# ── File Manager ──────────────────────────────────────────────────────────────

@app.route('/server/<int:server_id>/files')
@login_required
def files(server_id):
    server = db.session.get(Server, server_id) or abort(404)
    if not owns_server(server):
        abort(403)

    current_path = request.args.get('path', '').strip('/')
    if current_path:
        safe_subpath(server, current_path)  # validates path safety

    file_list  = dir_listing(server, subpath=current_path)
    disk_used  = server.disk_used_mb
    disk_limit = 'inf' if current_user.is_admin else current_user.disk_limit

    # Build breadcrumb: list of {'name': str, 'path': str}
    breadcrumbs = []
    if current_path:
        parts = current_path.split('/')
        for i, part in enumerate(parts):
            breadcrumbs.append({'name': part, 'path': '/'.join(parts[:i + 1])})

    return render_template('files.html', server=server,
                           files=file_list,
                           disk_used=disk_used,
                           disk_limit=disk_limit,
                           current_path=current_path,
                           breadcrumbs=breadcrumbs)


@app.route('/server/<int:server_id>/files/mkdir', methods=['POST'])
@login_required
def file_mkdir(server_id):
    server       = db.session.get(Server, server_id) or abort(404)
    if not owns_server(server):
        abort(403)
    folder_name  = secure_filename(request.form.get('folder_name', '').strip())
    current_path = request.form.get('current_path', '').strip('/')

    url = url_for('files', server_id=server_id)
    back = f'{url}?path={current_path}' if current_path else url

    if not folder_name:
        flash('Folder name is required.', 'error')
        return redirect(back)

    rel = f'{current_path}/{folder_name}'.lstrip('/') if current_path else folder_name
    target = safe_path(server, rel)
    if target.exists():
        flash(f'"{folder_name}" already exists.', 'error')
    else:
        target.mkdir(parents=True, exist_ok=True)
        server.append_log('INFO', f'Folder "{rel}" created by {current_user.username}')
        flash(f'Folder "{folder_name}" created.', 'success')
    return redirect(back)


@app.route('/server/<int:server_id>/files/delete-bulk', methods=['POST'])
@login_required
def file_delete_bulk(server_id):
    server       = db.session.get(Server, server_id) or abort(404)
    if not owns_server(server):
        abort(403)
    names        = request.form.getlist('filenames')
    current_path = request.form.get('current_path', '').strip('/')
    deleted      = 0
    for name in names:
        target = safe_path(server, name)
        if target.exists():
            shutil.rmtree(target) if target.is_dir() else target.unlink()
            deleted += 1
    if deleted:
        server.append_log('INFO', f'Bulk deleted {deleted} file(s) by {current_user.username}')
        flash(f'Deleted {deleted} file(s).', 'success')
    url = url_for('files', server_id=server_id)
    return redirect(f'{url}?path={current_path}' if current_path else url)


@app.route('/server/<int:server_id>/files/upload', methods=['POST'])
@login_required
def file_upload(server_id):
    server = db.session.get(Server, server_id) or abort(404)
    if not owns_server(server):
        abort(403)

    uploads = request.files.getlist('files')
    if not uploads:
        flash('No files selected.', 'error')
        return redirect(url_for('files', server_id=server_id))

    upload_path = request.form.get('upload_path', '').strip('/')
    dest_dir    = safe_subpath(server, upload_path) if upload_path else server.upload_dir

    owner  = server.owner
    max_mb = None if owner.is_admin else FREE_FILE_SIZE
    disk_mb = None if owner.is_admin else owner.disk_limit

    saved = 0
    for f in uploads:
        if not f.filename:
            continue
        fname = secure_filename(f.filename)
        if not fname:
            continue
        f.seek(0, 2)
        size_mb = f.tell() / (1024 * 1024)
        f.seek(0)
        if max_mb and size_mb > max_mb:
            flash(f'{fname} exceeds the {max_mb} MB file size limit.', 'error')
            continue
        if disk_mb and (server.disk_used_mb + size_mb) > disk_mb:
            flash(f'Disk quota ({disk_mb} MB) would be exceeded.', 'error')
            break
        f.save(str(dest_dir / fname))
        saved += 1
        server.append_log('UPLOAD', f'{fname} uploaded by {current_user.username}')

    if saved:
        flash(f'{saved} file(s) uploaded successfully.', 'success')
    url = url_for('files', server_id=server_id)
    return redirect(f'{url}?path={upload_path}' if upload_path else url)


@app.route('/server/<int:server_id>/files/rename', methods=['POST'])
@login_required
def file_rename(server_id):
    server       = db.session.get(Server, server_id) or abort(404)
    if not owns_server(server):
        abort(403)
    old_name     = request.form.get('old_name', '').strip()
    new_raw      = request.form.get('new_name', '').strip()
    current_path = request.form.get('current_path', '').strip('/')

    if not old_name or not new_raw:
        flash('Both names are required.', 'error')
        url = url_for('files', server_id=server_id)
        return redirect(f'{url}?path={current_path}' if current_path else url)

    new_base = secure_filename(new_raw)
    if not new_base:
        flash('Invalid file name.', 'error')
        url = url_for('files', server_id=server_id)
        return redirect(f'{url}?path={current_path}' if current_path else url)

    new_rel = f'{current_path}/{new_base}'.lstrip('/') if current_path else new_base
    src     = safe_path(server, old_name)
    dest    = safe_path(server, new_rel)
    if not src.exists():
        flash('File not found.', 'error')
    elif dest.exists():
        flash(f'{new_base} already exists.', 'error')
    else:
        src.rename(dest)
        flash(f'Renamed to {new_base}.', 'success')

    url = url_for('files', server_id=server_id)
    return redirect(f'{url}?path={current_path}' if current_path else url)


@app.route('/server/<int:server_id>/files/delete', methods=['POST'])
@login_required
def file_delete(server_id):
    server       = db.session.get(Server, server_id) or abort(404)
    if not owns_server(server):
        abort(403)
    fname        = request.form.get('filename', '').strip()
    current_path = request.form.get('current_path', '').strip('/')
    if not fname:
        abort(400)
    target = safe_path(server, fname)
    if not target.exists():
        flash('File not found.', 'error')
    else:
        shutil.rmtree(target) if target.is_dir() else target.unlink()
        flash(f'{Path(fname).name} deleted.', 'success')
    url = url_for('files', server_id=server_id)
    return redirect(f'{url}?path={current_path}' if current_path else url)


@app.route('/server/<int:server_id>/files/edit/<path:filename>', methods=['GET', 'POST'])
@login_required
def file_edit(server_id, filename):
    server = db.session.get(Server, server_id) or abort(404)
    if not owns_server(server):
        abort(403)
    target = safe_path(server, filename)
    if not target.exists() or not target.is_file():
        abort(404)

    ext = target.suffix.lower()
    parent_path = str(Path(filename).parent)
    back_path   = None if parent_path == '.' else parent_path

    if ext not in EDITABLE_EXT:
        flash('This file type cannot be edited in the browser.', 'error')
        url = url_for('files', server_id=server_id)
        return redirect(f'{url}?path={back_path}' if back_path else url)

    if request.method == 'POST':
        content = request.form.get('content', '')
        target.write_text(content, encoding='utf-8')
        flash(f'{Path(filename).name} saved.', 'success')
        return redirect(url_for('file_edit', server_id=server_id, filename=filename))

    try:
        content = target.read_text(encoding='utf-8')
    except Exception:
        flash('Could not read file.', 'error')
        url = url_for('files', server_id=server_id)
        return redirect(f'{url}?path={back_path}' if back_path else url)

    lang_map = {
        '.py': 'python', '.js': 'javascript', '.ts': 'typescript',
        '.json': 'json', '.yaml': 'yaml', '.yml': 'yaml',
        '.toml': 'toml', '.sh': 'bash', '.html': 'html', '.css': 'css',
        '.md': 'markdown', '.xml': 'xml',
    }
    return render_template('file_edit.html', server=server,
                           filename=filename, content=content,
                           lang=lang_map.get(ext, 'plaintext'))


@app.route('/server/<int:server_id>/files/extract/<path:filename>', methods=['POST'])
@login_required
def file_extract(server_id, filename):
    server       = db.session.get(Server, server_id) or abort(404)
    if not owns_server(server):
        abort(403)
    current_path = request.form.get('current_path', '').strip('/')
    target       = safe_path(server, filename)
    url          = url_for('files', server_id=server_id)
    back         = f'{url}?path={current_path}' if current_path else url

    if not target.exists() or target.suffix.lower() != '.zip':
        flash('File not found or not a ZIP archive.', 'error')
        return redirect(back)

    owner = server.owner
    if not owner.is_admin:
        try:
            with zipfile.ZipFile(target) as zf:
                uncompressed = sum(i.file_size for i in zf.infolist()) / (1024 * 1024)
            if (server.disk_used_mb + uncompressed) > owner.disk_limit:
                flash('Not enough disk quota to extract this archive.', 'error')
                return redirect(back)
        except Exception:
            pass

    try:
        with zipfile.ZipFile(target, 'r') as zf:
            for member in zf.infolist():
                mp = (server.upload_dir / member.filename).resolve()
                if not str(mp).startswith(str(server.upload_dir.resolve())):
                    continue
                zf.extract(member, server.upload_dir)
        flash(f'{Path(filename).name} extracted successfully.', 'success')
    except zipfile.BadZipFile:
        flash('Invalid ZIP file.', 'error')
    except Exception as e:
        flash(f'Extraction failed: {e}', 'error')
    return redirect(back)


@app.route('/server/<int:server_id>/files/download/<path:filename>')
@login_required
def file_download(server_id, filename):
    server = db.session.get(Server, server_id) or abort(404)
    if not owns_server(server):
        abort(403)
    target = safe_path(server, filename)
    if not target.exists() or not target.is_file():
        abort(404)
    return send_from_directory(server.upload_dir, filename, as_attachment=True)


# ── Admin ─────────────────────────────────────────────────────────────────────

@app.route('/admin')
@login_required
@admin_required
def admin_index():
    stats = {
        'total_users':     User.query.count(),
        'total_servers':   Server.query.count(),
        'running_servers': Server.query.filter_by(status='running').count(),
        'stopped_servers': Server.query.filter_by(status='stopped').count(),
        'python_servers':  Server.query.filter_by(bot_type='python').count(),
        'js_servers':      Server.query.filter_by(bot_type='js').count(),
    }
    return render_template('admin/index.html', stats=stats,
                           recent_users=User.query.order_by(User.created_at.desc()).limit(5).all(),
                           recent_servers=Server.query.order_by(Server.created_at.desc()).limit(5).all())


@app.route('/admin/users')
@login_required
@admin_required
def admin_users():
    return render_template('admin/users.html',
                           users=User.query.order_by(User.created_at.desc()).all())


@app.route('/admin/users/create', methods=['POST'])
@login_required
@admin_required
def admin_create_user():
    username      = request.form.get('username', '').strip()
    email         = request.form.get('email', '').strip().lower()
    password      = request.form.get('password', '')
    is_admin      = bool(request.form.get('is_admin'))
    create_server = bool(request.form.get('create_server'))

    if not username or not email or not password:
        flash('All fields are required.', 'error'); return redirect(url_for('admin_users'))
    if len(username) < 3:
        flash('Username must be at least 3 characters.', 'error'); return redirect(url_for('admin_users'))
    if len(password) < 6:
        flash('Password must be at least 6 characters.', 'error'); return redirect(url_for('admin_users'))
    if User.query.filter_by(username=username).first():
        flash('Username already taken.', 'error'); return redirect(url_for('admin_users'))
    if User.query.filter_by(email=email).first():
        flash('Email already registered.', 'error'); return redirect(url_for('admin_users'))

    user = User(username=username, email=email, is_admin=is_admin)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    if create_server:
        auto_create_server(user)
    flash(f'User {username} created.', 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/promote', methods=['POST'])
@login_required
@admin_required
def admin_promote_user(user_id):
    user = db.session.get(User, user_id) or abort(404)
    if user.id == current_user.id:
        flash("You can't change your own role.", 'error')
        return redirect(url_for('admin_users'))
    user.is_admin = not user.is_admin
    db.session.commit()
    flash(f'{user.username} {"promoted to admin" if user.is_admin else "demoted to free"}.', 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/limits', methods=['POST'])
@login_required
@admin_required
def admin_set_limits(user_id):
    user = db.session.get(User, user_id) or abort(404)
    try:
        user.cpu_limit  = max(1,  min(int(request.form.get('cpu_limit',  user.cpu_limit)),  100))
        user.ram_limit  = max(64, min(int(request.form.get('ram_limit',  user.ram_limit)),  16384))
        user.disk_limit = max(10, min(int(request.form.get('disk_limit', user.disk_limit)), 10240))
        db.session.commit()
        flash(f'Limits updated for {user.username}.', 'success')
    except ValueError:
        flash('Invalid limit values.', 'error')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/delete', methods=['POST'])
@login_required
@admin_required
def admin_delete_user(user_id):
    user = db.session.get(User, user_id) or abort(404)
    if user.id == current_user.id:
        flash("You can't delete yourself.", 'error')
        return redirect(url_for('admin_users'))
    for server in user.servers:
        ud = UPLOAD_BASE / str(server.id)
        if ud.exists():
            shutil.rmtree(ud)
    db.session.delete(user)
    db.session.commit()
    flash(f'User {user.username} deleted.', 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/servers')
@login_required
@admin_required
def admin_servers():
    return render_template('admin/servers.html',
                           servers=Server.query.order_by(Server.created_at.desc()).all())


@app.route('/admin/servers/<int:server_id>/toggle', methods=['POST'])
@login_required
@admin_required
def admin_toggle_server(server_id):
    server = db.session.get(Server, server_id) or abort(404)
    server.status = 'running' if server.status == 'stopped' else 'stopped'
    db.session.commit()
    event = 'START' if server.status == 'running' else 'STOP'
    server.append_log(event, f'Server {server.status} by admin {current_user.username}')
    flash(f'Server {server.name} is now {server.status}.', 'success')
    return redirect(url_for('admin_servers'))


@app.route('/admin/servers/<int:server_id>/delete', methods=['POST'])
@login_required
@admin_required
def admin_delete_server(server_id):
    server = db.session.get(Server, server_id) or abort(404)
    ud     = UPLOAD_BASE / str(server.id)
    if ud.exists():
        shutil.rmtree(ud)
    db.session.delete(server)
    db.session.commit()
    flash('Server deleted.', 'success')
    return redirect(url_for('admin_servers'))


# ── Error handlers ────────────────────────────────────────────────────────────

@app.errorhandler(403)
def forbidden(e):
    return render_template('error.html', code=403, message='Access denied.'), 403

@app.errorhandler(404)
def not_found(e):
    return render_template('error.html', code=404, message='Page not found.'), 404

@app.errorhandler(413)
def too_large(e):
    flash('File too large (100 MB hard limit).', 'error')
    return redirect(request.referrer or url_for('dashboard'))


# ── Init ──────────────────────────────────────────────────────────────────────

def init_db():
    with app.app_context():
        db.create_all()
        try:
            upgraded = db.session.execute(
                db.text("UPDATE bh_users SET disk_limit = 512 WHERE disk_limit = 100 AND is_admin = false")
            ).rowcount
            db.session.commit()
            if upgraded:
                print(f'[migrate] Upgraded {upgraded} user(s) disk limit 100->512 MB')
        except Exception as e:
            print(f'[migrate] disk_limit migration skipped: {e}')


if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port, debug=False)
