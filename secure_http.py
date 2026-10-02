"""HTTP transport which never redirects bearer credentials/OAuth bodies off-origin."""
import urllib.error
import urllib.parse
import urllib.request


def origin(url):
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme.lower(), parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)


class CredentialRedirectGuard(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        sensitive = request.data is not None or any(k.lower() == 'authorization' for k,v in request.header_items())
        if sensitive and origin(request.full_url) != origin(newurl):
            raise urllib.error.HTTPError(request.full_url,code,'refusing off-origin credential redirect',headers,fp)
        return super().redirect_request(request,fp,code,msg,headers,newurl)


def urlopen(request, timeout=30):
    return urllib.request.build_opener(CredentialRedirectGuard()).open(request,timeout=timeout)
