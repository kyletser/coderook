from __future__ import annotations

import argparse
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx


# 校验下载文件的官方内容摘要，缓存命中也必须先验证而不能仅凭文件名信任内容
def matches(path: Path, digest: str) -> bool:
    if not path.is_file():
        return False
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest() == digest.split(':')[1]


# 从官方 Docker Hub 下载已验证的 OCI blob，分段请求避免本地 Docker 拉取路径长时间停滞
def download_blob(client: httpx.Client, repository: str, descriptor: dict, cache: Path) -> Path:
    digest, size = descriptor['digest'], descriptor['size']
    target = cache / digest.split(':')[1]
    if matches(target, digest):
        print(f'Cached {digest[:19]}', flush=True)
        return target
    for sibling in cache.parents[2].glob('*/blobs/sha256/' + target.name):
        if sibling != target and matches(sibling, digest):
            target.hardlink_to(sibling)
            print(f'Reused {digest[:19]}', flush=True)
            return target
    auth = client.get('https://auth.docker.io/token', params={
        'service': 'registry.docker.io', 'scope': f'repository:{repository}:pull'})
    auth.raise_for_status()
    url = f'https://registry-1.docker.io/v2/{repository}/blobs/{digest}'
    headers = {'Authorization': 'Bearer ' + auth.json()['token']}
    probe = client.get(url, headers={**headers, 'Range': 'bytes=0-0'})
    probe.raise_for_status()
    remote = str(probe.url)
    # 只在重定向仍位于原 Registry 时携带 Registry token
    blob_headers = headers if probe.url.host == 'registry-1.docker.io' else {}
    chunk_size = 8 * 1024 * 1024
    parts = list(range(0, size, chunk_size))
    print(f'Downloading {digest[:19]} {size / 1e6:.1f} MB ({len(parts)} parts)', flush=True)

    # 为单个确定字节范围做有限重试，验证 Content-Range 防止服务器忽略 Range 后拼出错误镜像
    def fetch(start: int) -> Path:
        end = min(start + chunk_size, size) - 1
        part = target.with_suffix(f'.part-{start}')
        if part.is_file() and part.stat().st_size == end - start + 1:
            return part
        for attempt in range(3):
            try:
                content = bytearray()
                for offset in range(start, end + 1, 4 * 1024 * 1024):
                    last = min(offset + 4 * 1024 * 1024 - 1, end)
                    began = time.monotonic()
                    with client.stream('GET', remote, headers={
                        **blob_headers, 'Range': f'bytes={offset}-{last}'}, timeout=30) as response:
                        response.raise_for_status()
                        if response.status_code == 206:
                            if response.headers.get('content-range') != f'bytes {offset}-{last}/{size}':
                                raise RuntimeError('registry returned a mismatching byte range')
                        elif not (offset == 0 and last == size - 1):
                            raise RuntimeError('registry does not honor range requests')
                        before = len(content)
                        for chunk in response.iter_bytes():
                            content.extend(chunk)
                            if time.monotonic() - began > 30:
                                raise RuntimeError('range download exceeded 30 seconds')
                        if len(content) - before != last - offset + 1:
                            raise RuntimeError('registry returned a truncated byte range')
                part.write_bytes(content)
                return part
            except (httpx.HTTPError, RuntimeError):
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)
        raise AssertionError('unreachable')

    temporary = target.with_suffix('.assembling')
    with ThreadPoolExecutor(max_workers=6) as pool, temporary.open('wb') as output:
        for index, part in enumerate(pool.map(fetch, parts), 1):
            output.write(part.read_bytes())
            part.unlink()
            if index % 8 == 0 or index == len(parts):
                print(f'{digest[:19]} {index}/{len(parts)} parts', flush=True)
    if not matches(temporary, digest):
        raise RuntimeError('downloaded blob SHA256 does not match the official manifest')
    temporary.replace(target)
    return target


# 按官方平台 manifest 生成 OCI 布局供 regctl 导出及 docker load，不改动镜像任何层
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('image')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    repository, tag = args.image.rsplit(':', 1)
    if (not repository.startswith('swebench/') and repository != 'library/docker') or '/' in tag:
        raise SystemExit('only official SWE-bench and Docker CLI images are supported')
    root = args.output.resolve()
    blobs = root / 'blobs/sha256'
    blobs.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=90, follow_redirects=True) as client:
        auth = client.get('https://auth.docker.io/token', params={
            'service': 'registry.docker.io', 'scope': f'repository:{repository}:pull'})
        auth.raise_for_status()
        headers = {'Authorization': 'Bearer ' + auth.json()['token'],
                   'Accept': 'application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json'}
        base = f'https://registry-1.docker.io/v2/{repository}/manifests/'
        index_response = client.get(base + tag, headers=headers)
        index_response.raise_for_status()
        index = index_response.json()
        descriptor = next(item for item in index['manifests']
                          if item.get('platform', {}).get('os') == 'linux'
                          and item.get('platform', {}).get('architecture') == 'amd64')
        manifest_response = client.get(base + descriptor['digest'], headers=headers)
        manifest_response.raise_for_status()
        if 'sha256:' + hashlib.sha256(manifest_response.content).hexdigest() != descriptor['digest']:
            raise RuntimeError('official manifest digest mismatch')
        manifest = manifest_response.json()
        (blobs / descriptor['digest'].split(':')[1]).write_bytes(manifest_response.content)
        for blob in [manifest['config'], *manifest['layers']]:
            download_blob(client, repository, blob, blobs)
        descriptor = {**descriptor, 'annotations': {'org.opencontainers.image.ref.name': tag}}
        (root / 'index.json').write_text(json.dumps({'schemaVersion': 2, 'manifests': [descriptor]}), encoding='utf-8')
        (root / 'oci-layout').write_text('{"imageLayoutVersion":"1.0.0"}', encoding='utf-8')
        receipt = {'image': args.image, 'manifest_digest': descriptor['digest'],
                   'config_digest': manifest['config']['digest'],
                   'registry_index_sha256': hashlib.sha256(index_response.content).hexdigest(),
                   'all_blob_digests_verified': True}
        (root / 'download-receipt.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
        print(json.dumps(receipt, indent=2), flush=True)


if __name__ == '__main__':
    main()
