from datetime import datetime
import dateutil
import json
import logging
from uuid import uuid4

from django.http import HttpResponse
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.urls import reverse
from http import HTTPStatus

from catchformats.catch_webannotation_validator import \
    validate_format_catchanno as validate_input
from catchformats.errors import CatchFormatsError
from catchformats.errors import AnnotatorJSError

from .annojs import anno_to_annotatorjs
from .crud import CRUD
from .errors import AnnoError
from .errors import InvalidAnnotationCreatorError
from .errors import DuplicateAnnotationIdError
from .errors import MissingAnnotationError
from .errors import MissingAnnotationInputError
from .errors import UnknownOutputFormatError
from .search import query_username
from .search import query_userid
from .search import query_tags
from .search import query_target_medias
from .search import query_target_sources
from .models import Anno

import pdb

SCHEMA_VERSION = 'catch_v1.0'
CATCH_CONTEXT_IRI = 'http://catch-dev.harvardx.harvard.edu/catch-context.jsonld'
ANNOTATORJS_CONTEXT_IRI = 'http://annotatorjs.org'

CATCH_ANNO_FORMAT = 'CATCH_ANNO_FORMAT'
ANNOTATORJS_FORMAT = 'ANNOTATORJS_FORMAT'
OUTPUT_FORMATS = [CATCH_ANNO_FORMAT, ANNOTATORJS_FORMAT]
CATCH_OUTPUT_FORMAT_HTTPHEADER = 'HTTP_X_CATCH_OUTPUT_FORMAT'

logger = logging.getLogger(__name__)

def get_requesting_user(request):
    try:
        return request.catchjwt['userId']
    except Exception:
        # TODO: REMOVE FAKE
        return '1234567890'

def get_jwt_payload(request):
    try:
        return request.catchjwt
    except Exception:
        # TODO: REMOVE FAKE
        return {
            'userId': '123456789',
            'consumerKey': 'abc',
            'issuedAt': datetime.now(dateutil.tz.tzutc).replace(
                microsecond=0).isoformat(),
            'ttl': 60,
            'override': [],
            'error': '',
            'consumer': None,
        }


def get_default_permissions_for_user(user):
    return {
        'can_read': [],
        'can_update': [user],
        'can_delete': [user],
        'can_admin': [user],
    }

def get_input_json(request):
    if request.body:
        return json.loads(request.body)
    else:
        raise MissingAnnotationInputError(
            'missing json in body request for create/update')

def process_create(request, requesting_user, anno_id):
    # throws MissingAnnotationInputError
    a_input = get_input_json(request)

    # fill info for create-anno
    a_input['id'] = anno_id
    if 'permissions' not in a_input:
        a_input['permissions'] = get_default_permissions_for_user(
            requesting_user)
    if 'schema_version' not in a_input:
        a_input['schema_version'] = SCHEMA_VERSION


    # throws CatchFormatsError, AnnotatorJSError
    catcha = validate_input(a_input)

    # check for conflicts
    if catcha['creator']['id'] != requesting_user:
        raise InvalidAnnotationCreatorError(
            ('anno({}) conflict in input creator_id({}) does not match '
                'requesting_user({}) - not created').format(
                    anno_id, catcha['creator']['id'], requesting_user))
    # TODO: check if creator in permissions
    # TODO: check if reply to itself
    # TODO: check if annotation in targets if reply

    # throws AnnoError
    anno = CRUD.create_anno(catcha)
    return anno


def process_update(request, requesting_user, anno):
    # throws MissingAnnotationInputError
    a_input = get_input_json(request)

    # throws CatchFormatsERror, AnnotatorJSError
    catcha = validate_input(a_input)

    # throws AnnoError
    anno = CRUD.update_anno(anno, catcha, requesting_user)
    return anno


@require_http_methods(['GET', 'HEAD', 'POST', 'PUT', 'DELETE'])
def crud_api(request, anno_id):
    '''view to deal with crud api requests.'''
    try:
        resp = _do_crud_api(request, anno_id)
        status = HTTPStatus.OK
        if request.method == 'POST' or request.method == 'PUT':
            status = HTTPStatus.SEE_OTHER
        # add response header with location for new resource
        response = JsonResponse(status=status, data=resp)
        response['Location'] = request.build_absolute_uri(
            reverse('crudapi', kwargs={'anno_id': resp['id']}))
        return response

    except AnnoError as e:
        return JsonResponse(status=e.status,
                            data={'status': e.status, 'payload': [str(e)]})

    except (CatchFormatsError, AnnotatorJSError) as e:
        return JsonResponse(
            status=HTTPStatus.BAD_REQUEST,
            data={'status': HTTPStatus.BAD_REQUEST, 'payload': [str(e)]})

    except (ValueError, KeyError) as e:
        logger.error('anno({}): bad input:'.format(anno_id), exc_info=True)
        return JsonResponse(
            status=HTTPStatus.BAD_REQUEST,
            data={'status': HTTPStatus.BAD_REQUEST, 'payload': [str(e)]})


def _do_crud_api(request, anno_id):

    # assumes went through main auth and is ok

    # retrieves anno
    anno = CRUD.get_anno(anno_id)

    # TODO: while we don't have catch auth, fake requesting_user
    # the plan is to have it set in the request by middleware
    requesting_user = get_requesting_user(request)

    if anno is None:
        if request.method == 'POST':
            # sure there's no duplication and it's a create
            r = process_create(request, requesting_user, anno_id)
        else:
            raise MissingAnnotationError('anno({}) not found'.format(anno_id))
    else:
        # django strips body from response to HEAD requests
        # https://code.djangoproject.com/ticket/15668
        if request.method == 'GET' or request.method == 'HEAD':
            r = CRUD.read_anno(anno, requesting_user)
        elif request.method == 'DELETE':
            r = CRUD.delete_anno(anno, requesting_user)
        elif request.method == 'PUT':
            r = process_update(request, requesting_user, anno)
        elif request.method == 'POST':
            raise DuplicateAnnotationIdError(
                'anno({}): already exists, failed to create'.format(
                    anno.anno_id))

    assert r is not None

    # prep response
    output_format = CATCH_ANNO_FORMAT
    if CATCH_OUTPUT_FORMAT_HTTPHEADER in request.META:
        output_format = request.META[CATCH_OUTPUT_FORMAT_HTTPHEADER]

    if output_format == ANNOTATORJS_FORMAT:
        payload = anno_to_annotatorjs(r)

    elif output_format == CATCH_ANNO_FORMAT:
        # doesn't need formatting! SERIALIZE as webannotation
        payload = r.serialized
    else:
        # unknown format or plug custom formatters!
        raise UnknownOutputFormatError('unknown output format({})'.format(
            output_format))

    return payload



def partial_update_api(request, anno_id):
    pass


@require_http_methods(['GET', 'HEAD'])
def search_api(request):
    try:
        resp = _do_search_api(request)
        return JsonResponse(status=HTTPStatus.OK, data=resp)

    except (CatchFormatsError, AnnotatorJSError) as e:
        return JsonResponse(
            status=HTTPStatus.BAD_REQUEST,
            data={'status': HTTPStatus.BAD_REQUEST, 'payload': [str(e)]})

    except Exception as e:
        logger.error('anno({}): search failed:'.format(anno_id), exc_info=True)
        return JsonResponse(
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            data={'status': HTTPStatus.INTERNAL_SERVER_ERROR, 'payload': [str(e)]})



def _do_search_api(request):

    payload = get_jwt_payload(request)

    # filter out the soft-deleted
    query = Anno._default_manager.filter(anno_deleted=False)

    # TODO: check override POLICIES (override allow private reads)
    if 'CAN_READ' not in payload['override']:
        # filter out permission cannot_read
        query = query.filter(can_read__len=0).filter(        # public
            can_read__contains=[payload['userId']])  # allowed for user

    usernames = request.GET.get('username', [])
    if usernames:
        query = query.filter(query_username(usernames))

    userids = request.GET.get('userid', [])
    if userids:
        query = query.filter(query_userid(userids))

    tags = request.GET.get('tags', [])
    if tags:
        query = query.filter(query_tags(tags))

    targets = request.GET.get('target_source', [])
    if targets:
        query = query.filter(query_target_sources(targets))

    medias = request.GET.get('media', [])
    if medias:
        query = query.filter(query_target_medias(medias))

    text = request.GET.get('text', [])
    if text:
        query = query.filter(body_text__search=text)

    q = Anno.custom_manager.search_expression(request.GET)
    if q:
        query = query.filter(q)

    # sort by created date
    query = query.order_by('created')

    # max results and offset
    try:
        limit = int(request.GET.get('limit', 10))
    except ValueError:
        limit = 10

    try:
        offset = int(request.GET.get('offset', 0))
    except ValueError:
        offset = 0

    # check if limit -1 meaning complete result
    if limit < 0:
        q_result = query[offset:]
    else:
        q_result = query[offset:(offset+limit)]
    total = query.count()      # is it here when the querysets are evaluated?
    size = q_result.count()

    # prep response
    response = {
        'total': total,
        'size': size,
        'limit': limit,
        'offset': offset,
        'rows': [],
    }

    output_format = CATCH_ANNO_FORMAT
    if CATCH_OUTPUT_FORMAT_HTTPHEADER in request.META:
        output_format = request.META[CATCH_OUTPUT_FORMAT_HTTPHEADER]

    if output_format == ANNOTATORJS_FORMAT:
        for anno in q_result:
            annojs = anno_to_annotatorjs(anno)
            response['rows'].append(annojs)

    elif output_format == CATCH_ANNO_FORMAT:
        # doesn't need formatting! SERIALIZE as webannotation
        for anno in q_result:
            response['rows'].append(anno.serialized)
    else:
        # unknown format
        raise UnknownOutputFormatError('unknown output format({})'.format(
            output_format))

    return response


@require_http_methods(['GET', 'POST'])
def index(request):
    if request.method == 'POST':
        # create request without `id` in querystring, generate new `id`
        resp = crud_api(request, str(uuid4()))

        return resp
    else:
        # TODO: return info on the api
        return HttpResponse('Hello you. This is the annotation sample.')


@require_http_methods(['GET'])
def stash(request):
    filepath = request.GET.get('filepath', None)
    if filepath:
        with open(filepath, 'r') as fh:
            data = fh.read()
        catcha_list = json.loads(data)

    payload = get_jwt_payload(request)
    try:
        resp = CRUD.import_annos(catcha_list, payload)
        return JsonResponse(status=HTTPStatus.OK, data=resp)

    except AnnoError as e:
        return JsonResponse(status=e.status,
                            data={'status': e.status, 'payload': [str(e)]})

    except (CatchFormatsError, AnnotatorJSError) as e:
        return JsonResponse(
            status=HTTPStatus.BAD_REQUEST,
            data={'status': HTTPStatus.BAD_REQUEST, 'payload': [str(e)]})

    except (ValueError, KeyError) as e:
        logger.error('anno({}): bad input:'.format(anno_id), exc_info=True)
        return JsonResponse(
            status=HTTPStatus.BAD_REQUEST,
            data={'status': HTTPStatus.BAD_REQUEST, 'payload': [str(e)]})



def process_partial_update(request, requesting_user, anno_id):
    # assumes request.method == PUT
    return {
        'status': HTTPStatus.NOT_IMPLEMENTED,
        'payload': ['partial update not implemented.']}

    # retrieve anno

    # request user can update this?

    # validates -- no formatting here

    # performs update and save to database

    # needs formatting?
    pass

















