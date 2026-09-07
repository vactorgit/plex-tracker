from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import sqlite3
import os
from datetime import datetime
import requests

app = Flask(__name__)
app.secret_key = 'plex-tracker-secret-key-change-in-production'

# Hosting containers start from a blank filesystem on every deploy, so the
# database has to live on a mounted disk that outlives the container.
DB_PATH = os.environ.get('DB_PATH', 'plex_tracker.db')
USERS = ['Vactor', 'Jeff', 'Brad']

def init_db():
    """Initialize the database with tables."""
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS media (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        media_type TEXT NOT NULL,
        added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS watched (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        media_id INTEGER NOT NULL,
        user TEXT NOT NULL,
        watched_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (media_id) REFERENCES media(id),
        UNIQUE(media_id, user)
    )''')

    # Re-syncing must not clone the library, so a title can only appear once.
    # Existing rows are collapsed onto the lowest id before the index is added.
    c.execute('''DELETE FROM media WHERE id NOT IN (
        SELECT MIN(id) FROM media GROUP BY title, media_type
    )''')
    c.execute('DELETE FROM watched WHERE media_id NOT IN (SELECT id FROM media)')
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS media_unique ON media (title, media_type)')

    conn.commit()
    conn.close()

def get_db_connection():
    """Get a database connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

PLEX_HEADERS = {
    'X-Plex-Client-Identifier': 'plex-tracker',
    'X-Plex-Product': 'Plex Tracker',
    'X-Plex-Version': '1.0',
    'Accept': 'application/json',
}

def get_plex_token(username, password):
    """Authenticate with Plex. Returns (token, error_message)."""
    response = requests.post(
        'https://plex.tv/api/v2/users/signin',
        headers=PLEX_HEADERS,
        data={'login': username, 'password': password},
        timeout=20,
    )

    if response.status_code == 200 or response.status_code == 201:
        return response.json().get('authToken'), None

    message = 'Plex rejected the sign-in.'
    try:
        errors = response.json().get('errors') or []
        if errors:
            message = errors[0].get('message', message)
    except ValueError:
        pass

    if response.status_code == 401:
        message = message.rstrip('.') + '. If your Plex account has two-factor authentication on, append the 6-digit code to the end of your password.'
    return None, message

def reachable_connections(server):
    """Public connections for a server, best first.

    Remote (non-local) URIs only: this app runs in the cloud, so a LAN address
    from the Plex response is never routable. Relay is slow, so it goes last.
    """
    remote = [c for c in server.get('connections', []) if c.get('uri') and not c.get('local')]
    remote.sort(key=lambda c: bool(c.get('relay')))
    return [c['uri'] for c in remote]

def fetch_sections(base_url, headers):
    response = requests.get(f'{base_url}/library/sections', headers=headers, timeout=20)
    response.raise_for_status()
    return response.json().get('MediaContainer', {}).get('Directory', [])

def get_plex_library(token):
    """Fetch movies and shows from every Plex server. Returns (items, error)."""
    headers = dict(PLEX_HEADERS, **{'X-Plex-Token': token})

    response = requests.get(
        'https://plex.tv/api/v2/resources',
        headers=headers,
        params={'includeHttps': 1, 'includeRelay': 1},
        timeout=20,
    )
    if response.status_code != 200:
        return [], 'Signed in, but Plex would not list your servers.'

    servers = [r for r in response.json() if 'server' in (r.get('provides') or '')]
    if not servers:
        return [], 'No Plex Media Server is attached to this account.'

    items = []
    unreachable = []

    for server in servers:
        name = server.get('name', 'server')
        base_url = None
        for uri in reachable_connections(server):
            try:
                sections = fetch_sections(uri, headers)
                base_url = uri
                break
            except requests.RequestException:
                continue

        if not base_url:
            unreachable.append(name)
            continue

        for section in sections:
            kind = section.get('type')
            if kind not in ('movie', 'show'):
                continue
            media_type = 'Movie' if kind == 'movie' else 'TV Show'

            try:
                media_response = requests.get(
                    f'{base_url}/library/sections/{section.get("key")}/all',
                    headers=headers,
                    timeout=60,
                )
                media_response.raise_for_status()
                container = media_response.json().get('MediaContainer', {})
            except (requests.RequestException, ValueError):
                continue

            # Movies arrive under Video, shows under Directory.
            for entry in container.get('Video', []) + container.get('Directory', []):
                title = entry.get('title')
                if title:
                    items.append({'title': title, 'type': media_type})

    if not items:
        if unreachable:
            return [], f'Could not reach {", ".join(unreachable)}. Check that Remote Access is still enabled on the server.'
        return [], 'Connected to Plex, but found no movie or TV libraries.'

    return items, None

@app.route('/')
def index():
    if 'user' not in session:
        return redirect(url_for('login'))
    return render_template('index.html', user=session['user'], users=USERS)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = request.form.get('user')
        if user in USERS:
            session['user'] = user
            return redirect(url_for('index'))
    return render_template('login.html', users=USERS)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/api/plex-sync', methods=['POST'])
def plex_sync():
    """Sync Plex library into the tracker."""
    data = request.get_json()
    plex_username = data.get('plex_username')
    plex_password = data.get('plex_password')

    if not plex_username or not plex_password:
        return jsonify({'error': 'Missing credentials'}), 400

    try:
        token, auth_error = get_plex_token(plex_username, plex_password)
        if not token:
            return jsonify({'error': auth_error}), 401

        items, library_error = get_plex_library(token)
        if library_error:
            return jsonify({'error': library_error}), 400
    except requests.RequestException:
        return jsonify({'error': 'Could not reach Plex. Try again in a moment.'}), 502

    conn = get_db_connection()
    c = conn.cursor()

    added = 0
    for item in items:
        c.execute(
            'INSERT OR IGNORE INTO media (title, media_type) VALUES (?, ?)',
            (item['title'], item['type'])
        )
        added += c.rowcount

    conn.commit()
    conn.close()

    return jsonify({'success': True, 'added': added, 'found': len(items)})

@app.route('/api/media', methods=['GET'])
def get_media():
    """Get all media with watched status."""
    conn = get_db_connection()
    c = conn.cursor()

    c.execute('SELECT * FROM media ORDER BY added_date DESC')
    media_list = c.fetchall()

    result = []
    for media in media_list:
        c.execute('SELECT user FROM watched WHERE media_id = ?', (media['id'],))
        watched_by = [row['user'] for row in c.fetchall()]

        result.append({
            'id': media['id'],
            'title': media['title'],
            'type': media['media_type'],
            'added_date': media['added_date'],
            'watched_by': watched_by,
            'all_watched': len(watched_by) == len(USERS)
        })

    conn.close()
    return jsonify(result)

@app.route('/api/media', methods=['POST'])
def add_media():
    """Add a new movie or show."""
    data = request.get_json()
    title = data.get('title')
    media_type = data.get('type')

    if not title or not media_type:
        return jsonify({'error': 'Missing title or type'}), 400

    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT INTO media (title, media_type) VALUES (?, ?)', (title, media_type))
    conn.commit()
    media_id = c.lastrowid
    conn.close()

    return jsonify({'id': media_id, 'title': title, 'type': media_type}), 201

@app.route('/api/watched/<int:media_id>', methods=['POST'])
def mark_watched(media_id):
    """Mark a media as watched by the current user."""
    user = session.get('user')
    if not user:
        return jsonify({'error': 'Not logged in'}), 401

    conn = get_db_connection()
    c = conn.cursor()

    try:
        c.execute('INSERT INTO watched (media_id, user) VALUES (?, ?)', (media_id, user))
        conn.commit()
    except sqlite3.IntegrityError:
        pass

    conn.close()
    return jsonify({'success': True})

@app.route('/api/watched/<int:media_id>', methods=['DELETE'])
def unmark_watched(media_id):
    """Unmark a media as watched by the current user."""
    user = session.get('user')
    if not user:
        return jsonify({'error': 'Not logged in'}), 401

    conn = get_db_connection()
    c = conn.cursor()
    c.execute('DELETE FROM watched WHERE media_id = ? AND user = ?', (media_id, user))
    conn.commit()
    conn.close()

    return jsonify({'success': True})

@app.route('/api/media/<int:media_id>', methods=['DELETE'])
def delete_media(media_id):
    """Delete a media and all its watched entries."""
    conn = get_db_connection()
    c = conn.cursor()

    c.execute('DELETE FROM watched WHERE media_id = ?', (media_id,))
    c.execute('DELETE FROM media WHERE id = ?', (media_id,))
    conn.commit()
    conn.close()

    return jsonify({'success': True})

if __name__ == '__main__':
    import os
    init_db()
    port = int(os.environ.get('PORT', 5001))
    app.run(debug=False, host='0.0.0.0', port=port)
