"""Read campus benefit status through the running local workspace."""

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener


class LocalOnlyRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    parser = argparse.ArgumentParser(description='查询本机工具台的校园版福袋状态。')
    parser.add_argument('--port', type=int, default=5500, help='本机工具台端口，默认 5500')
    parser.add_argument('--login-id', help='工具台共享账号 ID，省略时显示账号列表')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('端口须在 1–65535 之间。')

    opener = build_opener(ProxyHandler({}), LocalOnlyRedirectHandler())
    base = f'http://127.0.0.1:{args.port}'
    if args.login_id:
        path = '/api/member-benefits/status?' + urlencode({'login_id': args.login_id})
    else:
        path = '/api/logins'

    try:
        with opener.open(base + path, timeout=120) as response:
            data = json.load(response)
    except HTTPError as error:
        if error.headers.get_content_type() == 'application/json':
            with error:
                data = json.load(error)
            print(data['error'], file=sys.stderr)
        else:
            print(f'工具台返回 HTTP {error.code}。', file=sys.stderr)
        return 1
    except (URLError, TimeoutError):
        print(f'无法连接本机工具台，请检查 {base} 是否正在运行。', file=sys.stderr)
        return 1

    if args.login_id:
        benefit = next((benefit for member in data['members'] for benefit in member['benefits']
                        if benefit.get('claim_flow') == 'campus'), None)
        if benefit is None:
            print('官方会员清单中没有此账号的校园版福袋。')
            return 0
        result = {key: benefit[key] for key in ('name', 'remaining', 'claimed')}
        result['offers'] = benefit.get('selection', {}).get('offers', [])
        for key in ('claim_pending', 'status_message', 'selection_error'):
            if key in benefit:
                result[key] = benefit[key]
    else:
        result = [{key: account[key] for key in ('id', 'phone_masked', 'status')}
                  for account in data['accounts']]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
