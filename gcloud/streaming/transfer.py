# pylint: skip-file
"""Upload and download support for apitools."""

import email.generator as email_generator
import email.mime.multipart as mime_multipart
import email.mime.nonmultipart as mime_nonmultipart
import mimetypes
import os

import six
from six.moves import http_client

from gcloud.streaming.buffered_stream import BufferedStream
from gcloud.streaming.exceptions import CommunicationError
from gcloud.streaming.exceptions import ConfigurationValueError
from gcloud.streaming.exceptions import HttpError
from gcloud.streaming.exceptions import InvalidDataError
from gcloud.streaming.exceptions import InvalidUserInputError
from gcloud.streaming.exceptions import NotFoundError
from gcloud.streaming.exceptions import TransferInvalidError
from gcloud.streaming.exceptions import TransferRetryError
from gcloud.streaming.exceptions import UserError
from gcloud.streaming.http_wrapper import get_http
from gcloud.streaming.http_wrapper import handle_http_exceptions
from gcloud.streaming.http_wrapper import make_api_request
from gcloud.streaming.http_wrapper import Request
from gcloud.streaming.http_wrapper import RESUME_INCOMPLETE
from gcloud.streaming.stream_slice import StreamSlice
from gcloud.streaming.util import acceptable_mime_type
from gcloud.streaming.util import type_check


RESUMABLE_UPLOAD_THRESHOLD = 5 << 20
SIMPLE_UPLOAD = 'simple'
RESUMABLE_UPLOAD = 'resumable'


class _Transfer(object):

    """Generic bits common to Uploads and Downloads."""

    def __init__(self, stream, close_stream=False, chunksize=None,
                 auto_transfer=True, http=None, num_retries=5):
        self.__bytes_http = None
        self.__close_stream = close_stream
        self.__http = http
        self.__stream = stream
        self.__url = None

        self.__num_retries = 5
        # Let the @property do validation
        self.num_retries = num_retries

        self.retry_func = handle_http_exceptions
        self.auto_transfer = auto_transfer
        self.chunksize = chunksize or 1048576

    def __repr__(self):
        return str(self)

    @property
    def close_stream(self):
        return self.__close_stream

    @property
    def http(self):
        return self.__http

    @property
    def bytes_http(self):
        return self.__bytes_http or self.http

    @bytes_http.setter
    def bytes_http(self, value):
        self.__bytes_http = value

    @property
    def num_retries(self):
        return self.__num_retries

    @num_retries.setter
    def num_retries(self, value):
        type_check(value, six.integer_types)
        if value < 0:
            raise InvalidDataError(
                'Cannot have negative value for num_retries')
        self.__num_retries = value

    @property
    def stream(self):
        return self.__stream

    @property
    def url(self):
        return self.__url

    def _initialize(self, http, url):
        """Initialize this download by setting self.http and self.url.

        We want the user to be able to override self.http by having set
        the value in the constructor; in that case, we ignore the provided
        http.

        Args:
          http: An httplib2.Http instance or None.
          url: The url for this transfer.

        Returns:
          None. Initializes self.
        """
        self._ensure_uninitialized()
        if self.http is None:
            self.__http = http or get_http()
        self.__url = url

    @property
    def initialized(self):
        return self.url is not None and self.http is not None

    def _ensure_initialized(self):
        if not self.initialized:
            raise TransferInvalidError(
                'Cannot use uninitialized %s', type(self).__name__)

    def _ensure_uninitialized(self):
        if self.initialized:
            raise TransferInvalidError(
                'Cannot re-initialize %s', type(self).__name__)

    def __del__(self):
        if self.__close_stream:
            self.__stream.close()


class Download(_Transfer):

    """Data for a single download.

    Public attributes:
      chunksize: default chunksize to use for transfers.
    """
    _ACCEPTABLE_STATUSES = set((
        http_client.OK,
        http_client.NO_CONTENT,
        http_client.PARTIAL_CONTENT,
        http_client.REQUESTED_RANGE_NOT_SATISFIABLE,
    ))

    def __init__(self, stream, **kwds):
        total_size = kwds.pop('total_size', None)
        super(Download, self).__init__(stream, **kwds)
        self._initial_response = None
        self.__progress = 0
        self.__total_size = total_size
        self.__encoding = None

    @classmethod
    def from_file(cls, filename, overwrite=False, auto_transfer=True, **kwds):
        """Create a new download object from a filename."""
        path = os.path.expanduser(filename)
        if os.path.exists(path) and not overwrite:
            raise InvalidUserInputError(
                'File %s exists and overwrite not specified' % path)
        return cls(open(path, 'wb'), close_stream=True,
                   auto_transfer=auto_transfer, **kwds)

    @classmethod
    def from_stream(cls, stream, auto_transfer=True, total_size=None, **kwds):
        """Create a new Download object from a stream."""
        return cls(stream, auto_transfer=auto_transfer, total_size=total_size,
                   **kwds)

    @property
    def progress(self):
        return self.__progress

    @property
    def total_size(self):
        return self.__total_size

    @property
    def encoding(self):
        return self.__encoding

    def __repr__(self):
        if not self.initialized:
            return 'Download (uninitialized)'
        else:
            return 'Download with %d/%s bytes transferred from url %s' % (
                self.progress, self.total_size, self.url)

    def configure_request(self, http_request, url_builder):
        url_builder.query_params['alt'] = 'media'
        # TODO(craigcitro): We need to send range requests because by
        # default httplib2 stores entire reponses in memory. Override
        # httplib2's download method (as gsutil does) so that this is not
        # necessary.
        http_request.headers['Range'] = 'bytes=0-%d' % (self.chunksize - 1,)

    def _set_total(self, info):
        if 'content-range' in info:
            _, _, total = info['content-range'].rpartition('/')
            if total != '*':
                self.__total_size = int(total)
        # Note "total_size is None" means we don't know it; if no size
        # info was returned on our initial range request, that means we
        # have a 0-byte file. (That last statement has been verified
        # empirically, but is not clearly documented anywhere.)
        if self.total_size is None:
            self.__total_size = 0

    def initialize_download(self, http_request, http=None, client=None):
        """Initialize this download by making a request.

        Args:
          http_request: The HttpRequest to use to initialize this download.
          http: The httplib2.Http instance for this request.
          client: If provided, let this client process the final URL before
              sending any additional requests. If client is provided and
              http is not, client.http will be used instead.
        """
        self._ensure_uninitialized()
        if http is None and client is None:
            raise UserError('Must provide client or http.')
        http = http or client.http
        if client is not None:
            http_request.url = client.FinalizeTransferUrl(http_request.url)
        url = http_request.url
        if self.auto_transfer:
            end_byte = self._compute_end_byte(0)
            self._set_range_header(http_request, 0, end_byte)
            response = make_api_request(
                self.bytes_http or http, http_request)
            if response.status_code not in self._ACCEPTABLE_STATUSES:
                raise HttpError.from_response(response)
            self._initial_response = response
            self._set_total(response.info)
            url = response.info.get('content-location', response.request_url)
        if client is not None:
            url = client.FinalizeTransferUrl(url)
        self._initialize(http, url)
        # Unless the user has requested otherwise, we want to just
        # go ahead and pump the bytes now.
        if self.auto_transfer:
            self.stream_file(use_chunks=True)

    def _normalize_start_end(self, start, end=None):
        if end is not None:
            if start < 0:
                raise TransferInvalidError(
                    'Cannot have end index with negative start index')
            elif start >= self.total_size:
                raise TransferInvalidError(
                    'Cannot have start index greater than total size')
            end = min(end, self.total_size - 1)
            if end < start:
                raise TransferInvalidError(
                    'Range requested with end[%s] < start[%s]' % (end, start))
            return start, end
        else:
            if start < 0:
                start = max(0, start + self.total_size)
            return start, self.total_size - 1

    def _set_range_header(self, request, start, end=None):
        if start < 0:
            request.headers['range'] = 'bytes=%d' % start
        elif end is None:
            request.headers['range'] = 'bytes=%d-' % start
        else:
            request.headers['range'] = 'bytes=%d-%d' % (start, end)

    def _compute_end_byte(self, start, end=None, use_chunks=True):
        """Compute the last byte to fetch for this request.

        This is all based on the HTTP spec for Range and
        Content-Range.

        Note that this is potentially confusing in several ways:
          * the value for the last byte is 0-based, eg "fetch 10 bytes
            from the beginning" would return 9 here.
          * if we have no information about size, and don't want to
            use the chunksize, we'll return None.
        See the tests for more examples.

        Args:
          start: byte to start at.
          end: (int or None, default: None) Suggested last byte.
          use_chunks: (bool, default: True) If False, ignore self.chunksize.

        Returns:
          Last byte to use in a Range header, or None.

        """
        end_byte = end

        if start < 0 and not self.total_size:
            return end_byte

        if use_chunks:
            alternate = start + self.chunksize - 1
            if end_byte is not None:
                end_byte = min(end_byte, alternate)
            else:
                end_byte = alternate

        if self.total_size:
            alternate = self.total_size - 1
            if end_byte is not None:
                end_byte = min(end_byte, alternate)
            else:
                end_byte = alternate

        return end_byte

    def _get_chunk(self, start, end):
        """Retrieve a chunk, and return the full response."""
        self._ensure_initialized()
        request = Request(url=self.url)
        self._set_range_header(request, start, end=end)
        return make_api_request(
            self.bytes_http, request, retry_func=self.retry_func,
            retries=self.num_retries)

    def _process_response(self, response):
        """Process response (by updating self and writing to self.stream)."""
        if response.status_code not in self._ACCEPTABLE_STATUSES:
            # We distinguish errors that mean we made a mistake in setting
            # up the transfer versus something we should attempt again.
            if response.status_code in (http_client.FORBIDDEN,
                                        http_client.NOT_FOUND):
                raise HttpError.from_response(response)
            else:
                raise TransferRetryError(response.content)
        if response.status_code in (http_client.OK,
                                    http_client.PARTIAL_CONTENT):
            self.stream.write(response.content)
            self.__progress += response.length
            if response.info and 'content-encoding' in response.info:
                # TODO(craigcitro): Handle the case where this changes over a
                # download.
                self.__encoding = response.info['content-encoding']
        elif response.status_code == http_client.NO_CONTENT:
            # It's important to write something to the stream for the case
            # of a 0-byte download to a file, as otherwise python won't
            # create the file.
            self.stream.write('')
        return response

    def get_range(self, start, end=None, use_chunks=True):
        """Retrieve a given byte range from this download, inclusive.

        Range must be of one of these three forms:
        * 0 <= start, end = None: Fetch from start to the end of the file.
        * 0 <= start <= end: Fetch the bytes from start to end.
        * start < 0, end = None: Fetch the last -start bytes of the file.

        (These variations correspond to those described in the HTTP 1.1
        protocol for range headers in RFC 2616, sec. 14.35.1.)

        Args:
          start: (int) Where to start fetching bytes. (See above.)
          end: (int, optional) Where to stop fetching bytes. (See above.)
          use_chunks: (bool, default: True) If False, ignore self.chunksize
              and fetch this range in a single request.

        Returns:
          None. Streams bytes into self.stream.
        """
        self._ensure_initialized()
        progress_end_normalized = False
        if self.total_size is not None:
            progress, end_byte = self._normalize_start_end(start, end)
            progress_end_normalized = True
        else:
            progress = start
            end_byte = end
        while (not progress_end_normalized or end_byte is None or
               progress <= end_byte):
            end_byte = self._compute_end_byte(progress, end=end_byte,
                                              use_chunks=use_chunks)
            response = self._get_chunk(progress, end_byte)
            if not progress_end_normalized:
                self._set_total(response.info)
                progress, end_byte = self._normalize_start_end(start, end)
                progress_end_normalized = True
            response = self._process_response(response)
            progress += response.length
            if response.length == 0:
                raise TransferRetryError(
                    'Zero bytes unexpectedly returned in download response')

    def stream_file(self, use_chunks=True):
        """Stream the entire download.

        Args:
          use_chunks: (bool, default: True) If False, ignore self.chunksize
              and stream this download in a single request.

        Returns:
            None. Streams bytes into self.stream.
        """
        self._ensure_initialized()
        while True:
            if self._initial_response is not None:
                response = self._initial_response
                self._initial_response = None
            else:
                end_byte = self._compute_end_byte(self.progress,
                                                  use_chunks=use_chunks)
                response = self._get_chunk(self.progress, end_byte)
            if self.total_size is None:
                self._set_total(response.info)
            response = self._process_response(response)
            if (response.status_code == http_client.OK or
                    self.progress >= self.total_size):
                break


class Upload(_Transfer):

    """Data for a single Upload.

    Fields:
      stream: The stream to upload.
      mime_type: MIME type of the upload.
      total_size: (optional) Total upload size for the stream.
      close_stream: (default: False) Whether or not we should close the
          stream when finished with the upload.
      auto_transfer: (default: True) If True, stream all bytes as soon as
          the upload is created.
    """
    _REQUIRED_SERIALIZATION_KEYS = set((
        'auto_transfer', 'mime_type', 'total_size', 'url'))

    def __init__(self, stream, mime_type, total_size=None, http=None,
                 close_stream=False, chunksize=None, auto_transfer=True,
                 **kwds):
        super(Upload, self).__init__(
            stream, close_stream=close_stream, chunksize=chunksize,
            auto_transfer=auto_transfer, http=http, **kwds)
        self._final_response = None
        self._server_chunk_granularity = None
        self._complete = False
        self.__mime_type = mime_type
        self.__progress = 0
        self.__strategy = None
        self.__total_size = total_size

    @classmethod
    def from_file(cls, filename, mime_type=None, auto_transfer=True, **kwds):
        """Create a new Upload object from a filename."""
        path = os.path.expanduser(filename)
        if not os.path.exists(path):
            raise NotFoundError('Could not find file %s' % path)
        if not mime_type:
            mime_type, _ = mimetypes.guess_type(path)
            if mime_type is None:
                raise InvalidUserInputError(
                    'Could not guess mime type for %s' % path)
        size = os.stat(path).st_size
        return cls(open(path, 'rb'), mime_type, total_size=size,
                   close_stream=True, auto_transfer=auto_transfer, **kwds)

    @classmethod
    def from_stream(cls, stream, mime_type,
                    total_size=None, auto_transfer=True, **kwds):
        """Create a new Upload object from a stream."""
        if mime_type is None:
            raise InvalidUserInputError(
                'No mime_type specified for stream')
        return cls(stream, mime_type, total_size=total_size,
                   close_stream=False, auto_transfer=auto_transfer, **kwds)

    @property
    def complete(self):
        return self._complete

    @property
    def mime_type(self):
        return self.__mime_type

    @property
    def progress(self):
        return self.__progress

    @property
    def strategy(self):
        return self.__strategy

    @strategy.setter
    def strategy(self, value):
        if value not in (SIMPLE_UPLOAD, RESUMABLE_UPLOAD):
            raise UserError((
                'Invalid value "%s" for upload strategy, must be one of '
                '"simple" or "resumable".') % value)
        self.__strategy = value

    @property
    def total_size(self):
        return self.__total_size

    @total_size.setter
    def total_size(self, value):
        self._ensure_uninitialized()
        self.__total_size = value

    def __repr__(self):
        if not self.initialized:
            return 'Upload (uninitialized)'
        else:
            return 'Upload with %d/%s bytes transferred for url %s' % (
                self.progress, self.total_size or '???', self.url)

    def _set_default_strategy(self, upload_config, http_request):
        """Determine and set the default upload strategy for this upload.

        We generally prefer simple or multipart, unless we're forced to
        use resumable. This happens when any of (1) the upload is too
        large, (2) the simple endpoint doesn't support multipart requests
        and we have metadata, or (3) there is no simple upload endpoint.

        Args:
          upload_config: Configuration for the upload endpoint.
          http_request: The associated http request.

        Returns:
          None.
        """
        if upload_config.resumable_path is None:
            self.strategy = SIMPLE_UPLOAD
        if self.strategy is not None:
            return
        strategy = SIMPLE_UPLOAD
        if (self.total_size is not None and
                self.total_size > RESUMABLE_UPLOAD_THRESHOLD):
            strategy = RESUMABLE_UPLOAD
        if http_request.body and not upload_config.simple_multipart:
            strategy = RESUMABLE_UPLOAD
        if not upload_config.simple_path:
            strategy = RESUMABLE_UPLOAD
        self.strategy = strategy

    def configure_request(self, upload_config, http_request, url_builder):
        """Configure the request and url for this upload."""
        # Validate total_size vs. max_size
        if (self.total_size and upload_config.max_size and
                self.total_size > upload_config.max_size):
            raise InvalidUserInputError(
                'Upload too big: %s larger than max size %s' % (
                    self.total_size, upload_config.max_size))
        # Validate mime type
        if not acceptable_mime_type(upload_config.accept, self.mime_type):
            raise InvalidUserInputError(
                'MIME type %s does not match any accepted MIME ranges %s' % (
                    self.mime_type, upload_config.accept))

        self._set_default_strategy(upload_config, http_request)
        if self.strategy == SIMPLE_UPLOAD:
            url_builder.relative_path = upload_config.simple_path
            if http_request.body:
                url_builder.query_params['uploadType'] = 'multipart'
                self._configure_multipart_request(http_request)
            else:
                url_builder.query_params['uploadType'] = 'media'
                self._configure_media_request(http_request)
        else:
            url_builder.relative_path = upload_config.resumable_path
            url_builder.query_params['uploadType'] = 'resumable'
            self._configure_resumable_request(http_request)

    def _configure_media_request(self, http_request):
        """Configure http_request as a simple request for this upload."""
        http_request.headers['content-type'] = self.mime_type
        http_request.body = self.stream.read()
        http_request.loggable_body = '<media body>'

    def _configure_multipart_request(self, http_request):
        """Configure http_request as a multipart request for this upload."""
        # This is a multipart/related upload.
        msg_root = mime_multipart.MIMEMultipart('related')
        # msg_root should not write out its own headers
        setattr(msg_root, '_write_headers', lambda self: None)

        # attach the body as one part
        msg = mime_nonmultipart.MIMENonMultipart(
            *http_request.headers['content-type'].split('/'))
        msg.set_payload(http_request.body)
        msg_root.attach(msg)

        # attach the media as the second part
        msg = mime_nonmultipart.MIMENonMultipart(*self.mime_type.split('/'))
        msg['Content-Transfer-Encoding'] = 'binary'
        msg.set_payload(self.stream.read())
        msg_root.attach(msg)

        # NOTE: We encode the body, but can't use
        #       `email.message.Message.as_string` because it prepends
        #       `> ` to `From ` lines.
        # NOTE: We must use six.StringIO() instead of io.StringIO() since the
        #       `email` library uses cStringIO in Py2 and io.StringIO in Py3.
        fp = six.StringIO()
        g = email_generator.Generator(fp, mangle_from_=False)
        g.flatten(msg_root, unixfrom=False)
        http_request.body = fp.getvalue()

        multipart_boundary = msg_root.get_boundary()
        http_request.headers['content-type'] = (
            'multipart/related; boundary="%s"' % multipart_boundary)

        body_components = http_request.body.split(multipart_boundary)
        headers, _, _ = body_components[-2].partition('\n\n')
        body_components[-2] = '\n\n'.join([headers, '<media body>\n\n--'])
        http_request.loggable_body = multipart_boundary.join(body_components)

    def _configure_resumable_request(self, http_request):
        http_request.headers['X-Upload-Content-Type'] = self.mime_type
        if self.total_size is not None:
            http_request.headers[
                'X-Upload-Content-Length'] = str(self.total_size)

    def refresh_upload_state(self):
        """Talk to the server and refresh the state of this resumable upload.

        Returns:
          Response if the upload is complete.
        """
        if self.strategy != RESUMABLE_UPLOAD:
            return
        self._ensure_initialized()
        # XXX Per RFC 2616/7231, a 'PUT' request is absolutely inappropriate
        # here: # it is intended to be used to replace the entire resource,
        # not to # query for a status.
        # If the back-end doesn't provide a way to query for this state
        # via a 'GET' request, somebody should be spanked.
        # http://www.w3.org/Protocols/rfc2616/rfc2616-sec9.html#sec9.6
        # http://tools.ietf.org/html/rfc7231#section-4.3.4
        # The violation is documented:
        # https://cloud.google.com/storage/docs/json_api/v1/how-tos/upload#resume-upload
        refresh_request = Request(
            url=self.url, http_method='PUT',
            headers={'Content-Range': 'bytes */*'})
        refresh_response = make_api_request(
            self.http, refresh_request, redirections=0,
            retries=self.num_retries)
        range_header = self._get_range_header(refresh_response)
        if refresh_response.status_code in (http_client.OK,
                                            http_client.CREATED):
            self._complete = True
            self.__progress = self.total_size
            self.stream.seek(self.progress)
            # If we're finished, the refresh response will contain the metadata
            # originally requested. Cache it so it can be returned in
            # StreamInChunks.
            self._final_response = refresh_response
        elif refresh_response.status_code == RESUME_INCOMPLETE:
            if range_header is None:
                self.__progress = 0
            else:
                self.__progress = self._last_byte(range_header) + 1
            self.stream.seek(self.progress)
        else:
            raise HttpError.from_response(refresh_response)

    def _get_range_header(self, response):
        # XXX Per RFC 2616/7233, 'Range' is a request header, not a response
        # header: # If the back-end is actually setting 'Range' on responses,
        # somebody should be spanked:  it should be sending 'Content-Range'
        # (including the # '/<length>' trailer).
        # http://www.w3.org/Protocols/rfc2616/rfc2616-sec14.html
        # http://tools.ietf.org/html/rfc7233#section-3.1
        # http://tools.ietf.org/html/rfc7233#section-4.2
        # The violation is documented:
        # https://cloud.google.com/storage/docs/json_api/v1/how-tos/upload#chunking
        return response.info.get('Range', response.info.get('range'))

    def initialize_upload(self, http_request, http=None, client=None):
        """Initialize this upload from the given http_request."""
        if self.strategy is None:
            raise UserError(
                'No upload strategy set; did you call configure_request?')
        if http is None and client is None:
            raise UserError('Must provide client or http.')
        if self.strategy != RESUMABLE_UPLOAD:
            return
        http = http or client.http
        if client is not None:
            http_request.url = client.FinalizeTransferUrl(http_request.url)
        self._ensure_uninitialized()
        http_response = make_api_request(http, http_request,
                                         retries=self.num_retries)
        if http_response.status_code != http_client.OK:
            raise HttpError.from_response(http_response)

        # XXX when is this getting converted to an integer?
        granularity = http_response.info.get('X-Goog-Upload-Chunk-Granularity')
        if granularity is not None:
            granularity = int(granularity)
        self._server_chunk_granularity = granularity
        url = http_response.info['location']
        if client is not None:
            url = client.FinalizeTransferUrl(url)
        self._initialize(http, url)

        # Unless the user has requested otherwise, we want to just
        # go ahead and pump the bytes now.
        if self.auto_transfer:
            return self.stream_file(use_chunks=True)
        else:
            return http_response

    def _last_byte(self, range_header):
        _, _, end = range_header.partition('-')
        # TODO(craigcitro): Validate start == 0?
        return int(end)

    def _validate_chunksize(self, chunksize=None):
        if self._server_chunk_granularity is None:
            return
        chunksize = chunksize or self.chunksize
        if chunksize % self._server_chunk_granularity:
            raise ConfigurationValueError(
                'Server requires chunksize to be a multiple of %d',
                self._server_chunk_granularity)

    def stream_file(self, use_chunks=True):
        """Send this resumable upload

        If 'use_chunks' is False, send it in a single request. Otherwise,
        send it in chunks.
        """
        if self.strategy != RESUMABLE_UPLOAD:
            raise InvalidUserInputError(
                'Cannot stream non-resumable upload')
        # final_response is set if we resumed an already-completed upload.
        response = self._final_response
        send_func = self._send_chunk if use_chunks else self._send_media_body
        if use_chunks:
            self._validate_chunksize(self.chunksize)
        self._ensure_initialized()
        while not self.complete:
            response = send_func(self.stream.tell())
            if response.status_code in (http_client.OK, http_client.CREATED):
                self._complete = True
                break
            self.__progress = self._last_byte(response.info['range'])
            if self.progress + 1 != self.stream.tell():
                # TODO(craigcitro): Add a better way to recover here.
                raise CommunicationError(
                    'Failed to transfer all bytes in chunk, upload paused at '
                    'byte %d' % self.progress)
        if self.complete and hasattr(self.stream, 'seek'):
            current_pos = self.stream.tell()
            self.stream.seek(0, os.SEEK_END)
            end_pos = self.stream.tell()
            self.stream.seek(current_pos)
            if current_pos != end_pos:
                raise TransferInvalidError(
                    'Upload complete with %s additional bytes left in stream' %
                    (int(end_pos) - int(current_pos)))
        return response

    def _send_media_request(self, request, end):
        """Request helper function for SendMediaBody & SendChunk."""
        response = make_api_request(
            self.bytes_http, request, retry_func=self.retry_func,
            retries=self.num_retries)
        if response.status_code not in (http_client.OK, http_client.CREATED,
                                        RESUME_INCOMPLETE):
            # We want to reset our state to wherever the server left us
            # before this failed request, and then raise.
            self.refresh_upload_state()
            raise HttpError.from_response(response)
        if response.status_code == RESUME_INCOMPLETE:
            last_byte = self._last_byte(
                self._get_range_header(response))
            if last_byte + 1 != end:
                self.stream.seek(last_byte)
        return response

    def _send_media_body(self, start):
        """Send the entire media stream in a single request."""
        self._ensure_initialized()
        if self.total_size is None:
            raise TransferInvalidError(
                'Total size must be known for SendMediaBody')
        body_stream = StreamSlice(self.stream, self.total_size - start)

        request = Request(url=self.url, http_method='PUT', body=body_stream)
        request.headers['Content-Type'] = self.mime_type
        if start == self.total_size:
            # End of an upload with 0 bytes left to send; just finalize.
            range_string = 'bytes */%s' % self.total_size
        else:
            range_string = 'bytes %s-%s/%s' % (start, self.total_size - 1,
                                               self.total_size)

        request.headers['Content-Range'] = range_string

        return self._send_media_request(request, self.total_size)

    def _send_chunk(self, start):
        """Send the specified chunk."""
        self._ensure_initialized()
        no_log_body = self.total_size is None
        if self.total_size is None:
            # For the streaming resumable case, we need to detect when
            # we're at the end of the stream.
            body_stream = BufferedStream(
                self.stream, start, self.chunksize)
            end = body_stream.stream_end_position
            if body_stream.stream_exhausted:
                self.__total_size = end
            # TODO: Here, change body_stream from a stream to a string object,
            # which means reading a chunk into memory.  This works around
            # https://code.google.com/p/httplib2/issues/detail?id=176 which can
            # cause httplib2 to skip bytes on 401's for file objects.
            # Rework this solution to be more general.
            body_stream = body_stream.read(self.chunksize)
        else:
            end = min(start + self.chunksize, self.total_size)
            body_stream = StreamSlice(self.stream, end - start)
        # TODO(craigcitro): Think about clearer errors on "no data in
        # stream".
        request = Request(url=self.url, http_method='PUT', body=body_stream)
        request.headers['Content-Type'] = self.mime_type
        if no_log_body:
            # Disable logging of streaming body.
            # TODO: Remove no_log_body and rework as part of a larger logs
            # refactor.
            request.loggable_body = '<media body>'
        if self.total_size is None:
            # Streaming resumable upload case, unknown total size.
            range_string = 'bytes %s-%s/*' % (start, end - 1)
        elif end == start:
            # End of an upload with 0 bytes left to send; just finalize.
            range_string = 'bytes */%s' % self.total_size
        else:
            # Normal resumable upload case with known sizes.
            range_string = 'bytes %s-%s/%s' % (start, end - 1, self.total_size)

        request.headers['Content-Range'] = range_string

        return self._send_media_request(request, end)
