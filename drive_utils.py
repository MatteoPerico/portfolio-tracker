"""Google Drive backup utilities for Portfolio Tracker."""
import io
import json
import os
import sqlite3
from datetime import datetime

_HERE        = os.path.dirname(os.path.abspath(__file__))
SCOPES       = ['https://www.googleapis.com/auth/drive.file']
TOKEN_PATH   = os.path.join(_HERE, 'token.json')
CREDS_PATH   = os.path.join(_HERE, 'credentials.json')
REDIRECT_URI = 'http://localhost:5001/api/drive/callback'
FOLDER_NAME  = 'Portfolio Tracker Backup'


def get_creds():
    """Return valid credentials or None if not authenticated."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not os.path.exists(TOKEN_PATH):
        return None
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(TOKEN_PATH, 'w') as f:
                f.write(creds.to_json())
            return creds
        except Exception:
            return None
    return None


def build_service(creds):
    from googleapiclient.discovery import build
    return build('drive', 'v3', credentials=creds)


def get_auth_url():
    """Return (auth_url, state, flow) to start the OAuth consent flow.
    The flow object must be kept alive until exchange_code() is called —
    it holds the PKCE code_verifier generated internally."""
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_secrets_file(CREDS_PATH, scopes=SCOPES,
                                         redirect_uri=REDIRECT_URI)
    auth_url, state = flow.authorization_url(access_type='offline',
                                              prompt='consent')
    return auth_url, state, flow


def exchange_code(flow, code):
    """Exchange authorisation code using the original flow (preserves PKCE verifier)."""
    flow.fetch_token(code=code)
    with open(TOKEN_PATH, 'w') as f:
        f.write(flow.credentials.to_json())


def revoke_creds():
    """Delete the saved token."""
    if os.path.exists(TOKEN_PATH):
        os.remove(TOKEN_PATH)


def get_or_create_folder(service):
    """Return the Drive folder ID, creating it if needed."""
    q = (f"name='{FOLDER_NAME}' "
         f"and mimeType='application/vnd.google-apps.folder' "
         f"and trashed=false")
    results = service.files().list(q=q, fields='files(id)').execute()
    files = results.get('files', [])
    if files:
        return files[0]['id']
    meta = {'name': FOLDER_NAME,
            'mimeType': 'application/vnd.google-apps.folder'}
    folder = service.files().create(body=meta, fields='id').execute()
    return folder['id']


def export_db(db_path):
    """Export all relevant tables to a plain dict."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    def rows(table):
        return [dict(r) for r in conn.execute(f'SELECT * FROM {table}').fetchall()]

    data = {
        'version':      2,
        'exported_at':  datetime.utcnow().isoformat() + 'Z',
        'transactions': rows('transactions'),
        'etf_info':     rows('etf_info'),
        'geo_allocation':  rows('geo_allocation'),
        'country_region':  rows('country_region'),
    }
    conn.close()
    return data


def upload_backup(service, folder_id, db_path):
    """Create a JSON snapshot and upload it to Drive. Returns file metadata."""
    from googleapiclient.http import MediaIoBaseUpload

    data    = export_db(db_path)
    ts      = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    fname   = f'portfolio_backup_{ts}.json'
    content = json.dumps(data, indent=2, default=str).encode()
    media   = MediaIoBaseUpload(io.BytesIO(content),
                                mimetype='application/json',
                                resumable=False)
    meta    = {'name': fname, 'parents': [folder_id]}
    return service.files().create(body=meta, media_body=media,
                                  fields='id,name,createdTime,size').execute()


def list_backups(service, folder_id):
    """Return the 20 most recent backup files (newest first)."""
    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        orderBy='createdTime desc',
        fields='files(id,name,createdTime,size)',
        pageSize=20,
    ).execute()
    return results.get('files', [])


def download_backup(service, file_id):
    """Download and parse a JSON backup file."""
    content = service.files().get_media(fileId=file_id).execute()
    return json.loads(content)
