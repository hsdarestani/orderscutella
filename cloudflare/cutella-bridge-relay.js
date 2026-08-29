const ORIGIN = 'https://cutellashop.ir/';

export default {
  async fetch(request) {
    if (request.method !== 'GET') {
      return new Response('Method not allowed', { status: 405 });
    }

    const incoming = new URL(request.url);
    const target = new URL(ORIGIN);
    target.search = incoming.search;

    const headers = new Headers();
    headers.set('Accept', 'application/json,text/plain,*/*');
    headers.set('Cache-Control', 'no-cache');
    headers.set('User-Agent', 'Cutella-Bridge-Relay/1.0');

    try {
      const upstream = await fetch(target.toString(), {
        method: 'GET',
        headers,
        redirect: 'follow',
        cf: { cacheTtl: 0, cacheEverything: false },
      });

      const responseHeaders = new Headers(upstream.headers);
      responseHeaders.set('Cache-Control', 'no-store');
      responseHeaders.set('X-Robots-Tag', 'noindex, nofollow');
      responseHeaders.set('X-Cutella-Relay', '1');

      return new Response(upstream.body, {
        status: upstream.status,
        statusText: upstream.statusText,
        headers: responseHeaders,
      });
    } catch (error) {
      return Response.json(
        { success: false, data: { message: 'Origin bridge unavailable' } },
        { status: 502, headers: { 'Cache-Control': 'no-store' } },
      );
    }
  },
};
