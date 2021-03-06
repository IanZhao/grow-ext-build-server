from google.appengine.api import memcache
from google.appengine.api import urlfetch
from google.appengine.ext import ndb
from googleapiclient import errors
from googleapiclient import discovery
from oauth2client.contrib import appengine
import cgi
import csv
import httplib2
import io
import logging
import time
import random
import os

RELOAD_ACL_QUERY_PARAM = 'grow-reload-acl'
SCOPE = 'https://www.googleapis.com/auth/drive'
EDIT_URL = 'https://docs.google.com/spreadsheets/d/{}'
RETRY_ERRORS = [
    'backendError',
    'internalServerError',
    'quotaExceeded',
    'userRateLimitExceeded',
]

discovery.logger.setLevel(logging.WARNING)

urlfetch.set_default_fetch_deadline(60)


class Error(Exception):
    pass


class Settings(ndb.Model):
    sheet_id = ndb.StringProperty()
    sheet_gid_global = ndb.StringProperty()
    sheet_gid_admins = ndb.StringProperty()

    @classmethod
    def instance(cls):
        key = ndb.Key(cls.__name__, 'Settings')
        ent = key.get()
        if ent is None:
            ent = cls(key=key)
            ent.put()
            logging.info('Created settings -> {}'.format(key))
        return ent


def get_query_dict():
    query_string = os.getenv('QUERY_STRING', '')
    return cgi.parse_qs(query_string, keep_blank_values=True)


def create_service(api='drive', version='v2'):
    credentials = appengine.AppAssertionCredentials(SCOPE)
    http = httplib2.Http()
    http = credentials.authorize(http)
    return discovery.build(api, version, http=http)


def _request_with_backoff(service, url):
    for n in range(0, 5):
        resp, content = service._http.request(url)
        if resp.status in [429]:
            logging.info('Attempt {} for {}'.format(n, url))
            logging.info(resp)
            time.sleep((2 ** (n + 1)) + random.random())
            continue
        return resp, content
    raise Error('Error {} {} downloading sheet: {}'.format(resp.status, resp.reason, url))


def _request_sheet_content(sheet_id, gid=None):
    service = create_service()
    logging.info('Loading ACL -> {}'.format(sheet_id))
    for n in range(0, 5):
        try:
            resp = service.files().get(fileId=sheet_id).execute()
        except errors.HttpError as error:
            if error.resp.reason in RETRY_ERRORS:
                logging.info('Attempt {} for {}'.format(n, url))
                time.sleep((2 ** (n + 1)) + random.random())
                continue
            raise
    if 'exportLinks' not in resp:
        raise Error('Nothing to export: {}'.format(sheet_id))
    for mimetype, url in resp['exportLinks'].iteritems():
        if not mimetype.endswith('csv'):
            continue
        if gid is not None:
            url += '&gid={}'.format(gid)
        resp, content = _request_with_backoff(service, url)
        if resp.status != 200:
            text = 'Error {} downloading sheet: {}:{}'
            text = text.format(resp.status, sheet_id, gid)
            raise Error(text)
        return content


def get_sheet(sheet_id, gid=None, use_cache=True):
    """Returns a list of rows from a sheet."""
    query_dict = get_query_dict()
    force_cache = RELOAD_ACL_QUERY_PARAM in query_dict
    cache_key = 'google_sheet:{}:{}'.format(sheet_id, gid)
    logging.info('Loading Google Sheet -> {}'.format(cache_key))
    result = memcache.get(cache_key)
    if result is None or force_cache or not use_cache:
        content = _request_sheet_content(sheet_id, gid=gid)
        fp = io.BytesIO()
        fp.write(content)
        fp.seek(0)
        reader = csv.DictReader(fp)
        result = [row for row in reader]
        logging.info('Saving Google Sheet in cache -> {}'.format(cache_key))
        memcache.set(cache_key, result)
    return result


def append_rows(sheet_id, gid, rows_to_append):
    rows = []
    for row in rows_to_append:
        values = []
        for item in row:
            values.append({
                'userEnteredValue': {
                    'stringValue': item,
                },
            })
        rows.append({'values': values})
    service = create_service(api='sheets', version='v4')
    requests = []
    requests.append({
        'appendCells': {
            'fields': 'userEnteredValue',
            'rows': rows,
            'sheetId': gid,
        },
    })
    body = {'requests': requests}
    resp = service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body=body).execute()


def get_spreadsheet_url(sheet_id, gid=None):
    url = 'https://docs.google.com/spreadsheets/d/{}'.format(sheet_id)
    if gid:
        url += '#gid={}'.format(gid)
    return url


def create_sheet(title='Untitled Grow Website Access'):
    service = create_service()
    data = {
      'title' : title,
      'mimeType' : 'application/vnd.google-apps.spreadsheet'
    }
    resp = service.files().insert(body=data, fields='id').execute()
    logging.info('Created sheet -> {}'.format(resp['id']))
    return resp['id']


def share_sheet(file_id, emails):
    service = create_service()
    for email in emails:
        permission = {
            'type': 'user',
            'role': 'writer',
            'value': email,
        }
        service.permissions().insert(
            fileId=file_id,
            body=permission,
            fields='id',
        ).execute()
        logging.info('Shared sheet -> {}'.format(email))


def get_or_create_sheet_from_settings(title=None, emails=None):
    settings = Settings.instance()
    if settings.sheet_id is None:
        if title:
            title = '{} Website Access'.format(title)
        sheet_id = create_sheet(title=title)
        share_sheet(sheet_id, emails)
        settings.sheet_id = sheet_id
        settings.put()
    sheet_id = settings.sheet_id
    sheet_gid_global = settings.sheet_gid_global
    resp = get_sheet(sheet_id, gid=sheet_gid_global)
    return resp
