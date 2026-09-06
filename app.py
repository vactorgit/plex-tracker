from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import sqlite3
import os
from datetime import datetime
import requests
import base64

app = Flask(__name__)
app.secret_key = 'plex-tracker-secret-key-change-in-production'

DB_PATH = 'plex_tracker.db'
USERS = ['Vactor', 'Jeff', 'Brad']

def init_db():
    """Initialize the database with tables."""
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

    conn.commit()
    conn.close()

def get_db_connection():
    """Get a database connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_plex_token(username, password):
    """Authenticate with Plex and get API token."""
    try:
        auth_str = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers = {
            'Authorization': f'Basic {auth_str}',
            'X-Plex-Client-Identifier': 'plex-tracker'
        }
        response = requests.post('https://plex.tv/users/sign-in.json', headers=headers, timeout=5)
        if response.status_code == 201:
            return response.json().get('user', {}).get('authentication-token')
        return None
    except:
        return None

def get_plex_library(token):
    """Fetch movies and shows from Plex library."""
    try:
        headers = {'X-Plex-Token': token}
        # Get all servers
        response = requests.get('https://plex.tv/api/resources', headers=headers, timeout=5)
        if response.status_code != 200:
            return []

        servers = response.json()
        items = []

        for server in servers:
            server_url = None
            for conn in server.get('connections', []):
                if conn.get('uri'):
                    server_url = conn.get('uri')
                    break

            if not server_url:
                continue

            # Get library sections
            try:
                lib_response = requests.get(f'{server_url}/library/sections', headers=headers, timeout=5)
                if lib_response.status_code != 200:
                    continue

                sections = lib_response.json().get('MediaContainer', {}).get('Directory', [])
                for section in sections:
                    section_type = section.get('type')
                    if section_type not in ['movie', 'show']:
                        continue

                    section_key = section.get('key')
                    media_type = 'Movie' if section_type == 'movie' else 'TV Show'

                    # Get all media in section
                    try:
                        media_response = requests.get(
                            f'{server_url}/library/sections/{section_key}/all',
                            headers=headers,
                            timeout=5,
                            params={'X-Plex-Token': token}
                        )
                        if media_response.status_code == 200:
                            media_container = media_response.json().get('MediaContainer', {})
                            for video in media_container.get('Video', []):
                                items.append({
                                    'title': video.get('title', 'Unknown'),
                                    'type': media_type
                                })
                    except:
                        pass
            except:
                pass

        return items
    except:
        return []

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

    # Get Plex token
    token = get_plex_token(plex_username, plex_password)
    if not token:
        return jsonify({'error': 'Invalid Plex credentials'}), 401

    # Get library items
    items = get_plex_library(token)

    if not items:
        return jsonify({'error': 'Could not fetch library or library is empty'}), 400

    # Add items to database
    conn = get_db_connection()
    c = conn.cursor()

    added = 0
    for item in items:
        try:
            c.execute(
                'INSERT INTO media (title, media_type) VALUES (?, ?)',
                (item['title'], item['type'])
            )
            added += 1
        except sqlite3.IntegrityError:
            pass  # Skip duplicates

    conn.commit()
    conn.close()

    return jsonify({'success': True, 'added': added})

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
