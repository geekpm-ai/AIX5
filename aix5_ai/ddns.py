"""Small, provider-based DDNS client inspired by ddns-go.

The updater keeps provider adapters explicit and auditable. It supports
Cloudflare, DNSPod, AliDNS, Tencent Cloud DNSPod, Baidu BCE DNS, and Huawei
Cloud DNS, updating A and AAAA records independently without exposing the
configured credentials to the UI.
"""

from __future__ import annotations

import ipaddress
import base64
import hashlib
import hmac
import re
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit, parse_qsl

import httpx

from .network import snapshot as network_snapshot

PUBLIC_IP_ENDPOINTS = {
    "ipv4": "https://api.ipify.org?format=json",
    "ipv6": "https://api6.ipify.org?format=json",
}
SUPPORTED_PROVIDERS = {
    "cloudflare": "Cloudflare",
    "dnspod": "DNSPod",
    "alidns": "阿里云 DNS",
    "tencentcloud": "腾讯云 DNS",
    "baiducloud": "百度云 DNS",
    "huaweicloud": "华为云 DNS",
}
DDNS_PROVIDER_CATALOG = {
    "cloudflare": "Cloudflare",
    "dnspod": "DNSPod",
    "alidns": "阿里云 DNS",
    "tencentcloud": "腾讯云 DNS",
    "trafficroute": "Traffic Route",
    "aliesa": "阿里云 ESA",
    "huaweicloud": "华为云 DNS",
    "baiducloud": "百度云 DNS",
    "callback": "Webhook 回调",
    "cloudns": "ClouDNS",
    "desec": "deSEC",
    "dnsla": "DNS.LA",
    "dynadot": "Dynadot",
    "dynv6": "dynv6",
    "edgeone": "腾讯 EdgeOne",
    "eranet": "Eranet",
    "gcore": "Gcore",
    "godaddy": "GoDaddy",
    "hipmdns": "HipMDNSMgr",
    "name_com": "Name.com",
    "namecheap": "Namecheap",
    "namesilo": "NameSilo",
    "nowcn": "Now.cn",
    "nsone": "NS1",
    "porkbun": "Porkbun",
    "rainyun": "雨云 Rainyun",
    "spaceship": "Spaceship",
    "tnethk": "TNetHK",
    "vercel": "Vercel",
}


def validate_hostname(value: str) -> str:
    value = value.strip().rstrip(".").lower()
    if len(value) > 253 or not value or "." not in value:
        raise ValueError("请输入完整域名，例如 camera.example.com")
    if any(len(part) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", part) for part in value.split(".")):
        raise ValueError("域名格式无效")
    return value


def validate_ip(value: str, version: int) -> str:
    address = ipaddress.ip_address(value)
    if address.version != version:
        raise ValueError("IP 地址版本与记录类型不匹配")
    return str(address)


async def discover_public_addresses(client: httpx.AsyncClient | None = None) -> dict[str, str | None]:
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=8, follow_redirects=True)
    result: dict[str, str | None] = {"ipv4": None, "ipv6": None}
    try:
        for version, url in PUBLIC_IP_ENDPOINTS.items():
            try:
                response = await client.get(url)
                response.raise_for_status()
                value = response.json().get("ip")
                result[version] = validate_ip(value, 4 if version == "ipv4" else 6)
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                continue
    finally:
        if owns_client:
            await client.aclose()
    return result


@dataclass
class UpdateResult:
    provider: str
    hostname: str
    addresses: dict[str, str | None]
    changed: list[str]
    skipped: list[str]
    unchanged: list[str]


class DdnsClient:
    def __init__(self, *, http_client: httpx.AsyncClient | None = None):
        self.http_client = http_client

    async def update(self, config: dict, credential: str | dict, *, addresses: dict | None = None, previous: dict | None = None) -> UpdateResult:
        provider = config["provider"]
        hostname = validate_hostname(config["hostname"])
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError("暂不支持该 DNS 服务商")
        if not credential:
            raise ValueError("请填写 DNS 凭据")
        if isinstance(credential, str):
            credential = {"access_key_secret": credential, "legacy": credential}
        if any(len(str(value)) > 2048 for value in credential.values() if value is not None):
            raise ValueError("DNS 凭据过长")
        owns_client = self.http_client is None
        client = self.http_client or httpx.AsyncClient(timeout=15, follow_redirects=True)
        try:
            addresses = addresses or await discover_public_addresses(client)
            selected = {
                "ipv4": (config.get("ipv4_address") or addresses.get("ipv4")) if config.get("ipv4", True) else None,
                "ipv6": (config.get("ipv6_address") or addresses.get("ipv6")) if config.get("ipv6", True) else None,
            }
            if not any(selected.values()):
                raise RuntimeError("没有发现可更新的公网 IPv4 或 IPv6 地址")
            changed: list[str] = []
            skipped: list[str] = []
            unchanged: list[str] = []
            for record_type, value in selected.items():
                if not value:
                    skipped.append(record_type.upper())
                    continue
                if previous and previous.get(record_type) == value:
                    unchanged.append(record_type.upper())
                    continue
                if provider == "cloudflare":
                    await self._cloudflare_record(client, str(credential.get("access_key_id") or credential.get("access_key_secret") or credential.get("legacy") or ""), hostname, record_type.upper(), value)
                elif provider == "dnspod":
                    await self._dnspod_record(client, f"{credential.get('access_key_id','')},{credential.get('access_key_secret','')}" if credential.get("access_key_id") else str(credential.get("access_key_secret") or credential.get("legacy") or ""), hostname, record_type.upper(), value)
                elif provider == "tencentcloud":
                    await self._tencent_record(client, str(credential.get("access_key_id") or ""), str(credential.get("access_key_secret") or ""), hostname, record_type.upper(), value)
                elif provider == "baiducloud":
                    await self._baidu_record(client, str(credential.get("access_key_id") or ""), str(credential.get("access_key_secret") or ""), hostname, record_type.upper(), value)
                elif provider == "huaweicloud":
                    await self._huaweicloud_record(client, str(credential.get("access_key_id") or ""), str(credential.get("access_key_secret") or ""), hostname, record_type.upper(), value)
                else:
                    await self._alidns_record(client, f"{credential.get('access_key_id','')},{credential.get('access_key_secret','')}" if credential.get("access_key_id") else str(credential.get("access_key_secret") or credential.get("legacy") or ""), hostname, record_type.upper(), value)
                changed.append(record_type.upper())
            return UpdateResult(provider, hostname, selected, changed, skipped, unchanged)
        finally:
            if owns_client:
                await client.aclose()

    @staticmethod
    async def _cloudflare_record(client, token: str, hostname: str, record_type: str, value: str):
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        labels = hostname.split(".")
        zone = None
        for index in range(len(labels) - 1):
            candidate = ".".join(labels[index:])
            response = await client.get("https://api.cloudflare.com/client/v4/zones", params={"name": candidate, "status": "active", "per_page": 1}, headers=headers)
            response.raise_for_status()
            result = response.json()
            if result.get("success") and result.get("result"):
                zone = result["result"][0]
                break
        if zone is None:
            raise RuntimeError("Cloudflare 找不到域名所在 Zone")
        params = {"type": record_type, "name": hostname, "per_page": 1}
        response = await client.get(f"https://api.cloudflare.com/client/v4/zones/{zone['id']}/dns_records", params=params, headers=headers)
        response.raise_for_status()
        records = response.json().get("result", [])
        payload = {"type": record_type, "name": hostname, "content": value, "ttl": 1, "proxied": False}
        if records:
            record_id = records[0]["id"]
            response = await client.put(f"https://api.cloudflare.com/client/v4/zones/{zone['id']}/dns_records/{record_id}", json=payload, headers=headers)
        else:
            response = await client.post(f"https://api.cloudflare.com/client/v4/zones/{zone['id']}/dns_records", json=payload, headers=headers)
        response.raise_for_status()
        if not response.json().get("success"):
            raise RuntimeError("Cloudflare 更新 DNS 记录失败")

    @staticmethod
    async def _dnspod_record(client, credential: str, hostname: str, record_type: str, value: str):
        if "," not in credential:
            raise ValueError("DNSPod 凭据格式应为 Token ID,Token")
        login_token = credential.strip()
        labels = hostname.split(".")
        domain = ".".join(labels[-2:])
        sub_domain = ".".join(labels[:-2]) or "@"
        data = {"login_token": login_token, "format": "json", "lang": "en", "error_on_empty": "yes", "domain": domain, "record_type": record_type, "record_line": "默认"}
        response = await client.post("https://dnsapi.cn/Record.List", data=data)
        response.raise_for_status()
        result = response.json()
        if result.get("status", {}).get("code") != "1":
            raise RuntimeError(result.get("status", {}).get("message", "DNSPod 查询失败"))
        record = next((item for item in result.get("records", []) if item.get("name") == sub_domain and item.get("type") == record_type), None)
        if record and record.get("value") == value:
            return
        payload = {**data, "sub_domain": sub_domain, "value": value, "record_line_id": "0", "status": "enable"}
        if record:
            payload["record_id"] = record["id"]
            endpoint = "Record.Modify"
        else:
            endpoint = "Record.Create"
        response = await client.post(f"https://dnsapi.cn/{endpoint}", data=payload)
        response.raise_for_status()
        result = response.json()
        if result.get("status", {}).get("code") != "1":
            raise RuntimeError(result.get("status", {}).get("message", "DNSPod 更新失败"))

    @staticmethod
    async def _alidns_record(client, credential: str, hostname: str, record_type: str, value: str):
        """Update AliDNS using the public RPC HMAC-SHA1 signing scheme."""

        if "," not in credential:
            raise ValueError("阿里云凭据格式应为 AccessKey ID,AccessKey Secret")
        access_key, secret = (part.strip() for part in credential.split(",", 1))
        labels = hostname.split(".")
        domain = ".".join(labels[-2:])
        rr = ".".join(labels[:-2]) or "@"

        async def request(action: str, extra: dict[str, str]):
            params = {
                "AccessKeyId": access_key,
                "Action": action,
                "Format": "JSON",
                "SignatureMethod": "HMAC-SHA1",
                "SignatureNonce": uuid.uuid4().hex,
                "SignatureVersion": "1.0",
                "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "Version": "2015-01-09",
                **extra,
            }
            canonical = "&".join(
                f"{quote(str(key), safe='')}={quote(str(params[key]), safe='~')}"
                for key in sorted(params)
            )
            string_to_sign = "GET&%2F&" + quote(canonical, safe="~")
            signature = base64.b64encode(
                hmac.new((secret + "&").encode(), string_to_sign.encode(), hashlib.sha1).digest()
            ).decode()
            params["Signature"] = signature
            response = await client.get("https://alidns.aliyuncs.com/", params=params)
            response.raise_for_status()
            payload = response.json()
            if payload.get("Code"):
                raise RuntimeError(payload.get("Message", "阿里云 DNS 更新失败"))
            return payload

        records = await request("DescribeDomainRecords", {"DomainName": domain, "RRKeyWord": rr, "TypeKeyWord": record_type})
        record = next(
            (item for item in records.get("DomainRecords", {}).get("Record", []) if item.get("RR") == rr and item.get("Type") == record_type),
            None,
        )
        if record and record.get("Value") == value:
            return
        if record:
            await request("UpdateDomainRecord", {"RecordId": record["RecordId"], "RR": rr, "Type": record_type, "Value": value})
        else:
            await request("AddDomainRecord", {"DomainName": domain, "RR": rr, "Type": record_type, "Value": value})

    @staticmethod
    async def _tencent_record(client, access_id: str, secret: str, hostname: str, record_type: str, value: str):
        """Tencent Cloud DNSPod API 3.0 (TC3-HMAC-SHA256)."""

        if not access_id or not secret:
            raise ValueError("腾讯云需要 SecretId 和 SecretKey")
        import hashlib as _hashlib
        import json as _json
        from datetime import datetime, timezone

        domain = ".".join(hostname.split(".")[-2:])
        subdomain = ".".join(hostname.split(".")[:-2]) or "@"
        endpoint = "dnspod.tencentcloudapi.com"
        service, version, action = "dnspod", "2021-03-23", "DescribeRecordList"
        payload = _json.dumps({"Domain": domain, "Subdomain": subdomain, "RecordType": record_type, "RecordLine": "默认"}, separators=(",", ":"))
        timestamp = int(time.time())
        date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
        hashed = _hashlib.sha256(payload.encode()).hexdigest()
        canonical = f"content-type:application/json\nhost:{endpoint}\nx-tc-action:{action.lower()}\n\ncontent-type;host;x-tc-action\n{hashed}"
        credential_scope = f"{date}/{service}/tc3_request"
        string_to_sign = f"TC3-HMAC-SHA256\n{timestamp}\n{credential_scope}\n{_hashlib.sha256(canonical.encode()).hexdigest()}"
        def sign(key, text): return hmac.new(key, text.encode(), _hashlib.sha256).digest()
        signing_key = sign(sign(sign(("TC3" + secret).encode(), date), service), "tc3_request")
        signature = hmac.new(signing_key, string_to_sign.encode(), _hashlib.sha256).hexdigest()
        headers = {"Content-Type":"application/json", "Host":endpoint, "X-TC-Action":action, "X-TC-Version":version, "X-TC-Timestamp":str(timestamp), "Authorization":f"TC3-HMAC-SHA256 Credential={access_id}/{credential_scope}, SignedHeaders=content-type;host;x-tc-action, Signature={signature}"}
        response = await client.post("https://"+endpoint, content=payload, headers=headers);response.raise_for_status();data=response.json()
        records=data.get("Response",{}).get("RecordList",[]);record=records[0] if records else None
        action="ModifyRecord" if record else "CreateRecord"
        body={"Domain":domain,"SubDomain":subdomain,"RecordType":record_type,"RecordLine":"默认","Value":value,"TTL":600}
        if record:body["RecordId"]=record.get("RecordId")
        if record and record.get("Value")==value:return
        # The signing helper is kept local for the second request; same canonical flow.
        payload=_json.dumps(body,separators=(",",":")); hashed=_hashlib.sha256(payload.encode()).hexdigest(); canonical=f"content-type:application/json\nhost:{endpoint}\nx-tc-action:{action.lower()}\n\ncontent-type;host;x-tc-action\n{hashed}"; credential_scope=f"{date}/{service}/tc3_request"; string_to_sign=f"TC3-HMAC-SHA256\n{timestamp}\n{credential_scope}\n{_hashlib.sha256(canonical.encode()).hexdigest()}";signature=hmac.new(signing_key,string_to_sign.encode(),_hashlib.sha256).hexdigest();headers.update({"X-TC-Action":action,"Authorization":f"TC3-HMAC-SHA256 Credential={access_id}/{credential_scope}, SignedHeaders=content-type;host;x-tc-action, Signature={signature}"})
        response=await client.post("https://"+endpoint,content=payload,headers=headers);response.raise_for_status();error=response.json().get("Response",{}).get("Error");
        if error:raise RuntimeError(error.get("Message","腾讯云 DNS 更新失败"))

    @staticmethod
    async def _baidu_record(client, access_id: str, secret: str, hostname: str, record_type: str, value: str):
        if not access_id or not secret: raise ValueError("百度云需要 Access Key 和 Secret Key")
        domain=".".join(hostname.split(".")[-2:]); rr=".".join(hostname.split(".")[:-2]) or "@"
        headers={"Content-Type":"application/json"}
        def signed_headers(path: str, method: str, body: dict):
            import json as _json
            payload=_json.dumps(body,separators=(",",":"))
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
            prefix=f"bce-auth-v1/{access_id}/{timestamp}/1800"
            canonical=f"{method}\n{path}\n\nhost:bcd.baidubce.com"
            key=hmac.new(secret.encode(),prefix.encode(),hashlib.sha256).hexdigest()
            signature=hmac.new(key.encode(),canonical.encode(),hashlib.sha256).hexdigest()
            return {**headers,"Host":"bcd.baidubce.com","Authorization":f"{prefix}/host/{signature}"},payload
        list_body={"domain":domain,"pageNum":1,"pageSize":1000};headers,payload=signed_headers("/v1/domain/resolve/list","POST",list_body);response=await client.post("https://bcd.baidubce.com/v1/domain/resolve/list",content=payload,headers=headers);response.raise_for_status();records=response.json().get("result",[]);record=next((item for item in records if item.get("domain")==rr and item.get("rdtype")==record_type),None)
        if record and record.get("rdata")==value:return
        if record:url="https://bcd.baidubce.com/v1/domain/resolve/edit";path="/v1/domain/resolve/edit";payload={"recordId":record["recordId"],"domain":rr,"view":record.get("view","default"),"rdType":record_type,"ttl":300,"rdata":value,"zoneName":record.get("zoneName",domain)}
        else:url="https://bcd.baidubce.com/v1/domain/resolve/add";path="/v1/domain/resolve/add";payload={"domain":rr,"rdType":record_type,"ttl":300,"rdata":value,"zoneName":domain}
        headers,body=signed_headers(path,"POST",payload);response=await client.post(url,content=body,headers=headers);response.raise_for_status()

    @staticmethod
    async def _huaweicloud_record(client, access_id: str, secret: str, hostname: str, record_type: str, value: str):
        if not access_id or not secret: raise ValueError("华为云需要 Access Key 和 Secret Key")
        domain=".".join(hostname.split(".")[-2:])
        async def request(method: str, url: str, body: dict | None = None):
            import json as _json
            parts=urlsplit(url);payload=_json.dumps(body,separators=(",",":")) if body is not None else "";when=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ");headers={"Host":parts.netloc,"X-Sdk-Date":when,"Content-Type":"application/json"}
            parsed_query=sorted(parse_qsl(parts.query,keep_blank_values=True));query="&".join(f"{quote(k,safe='~')}={quote(v,safe='~')}" for k,v in parsed_query);canonical_headers=f"content-type:{headers['Content-Type']}\nhost:{headers['Host']}\nx-sdk-date:{when}\n";signed="content-type;host;x-sdk-date";canonical=f"{method}\n{parts.path or '/'}\n{query}\n{canonical_headers}\n{signed}\n{hashlib.sha256(payload.encode()).hexdigest()}";string_to_sign=f"SDK-HMAC-SHA256\n{when}\n{hashlib.sha256(canonical.encode()).hexdigest()}";signature=hmac.new(secret.encode(),string_to_sign.encode(),hashlib.sha256).hexdigest();headers['Authorization']=f"SDK-HMAC-SHA256 Access={access_id}, SignedHeaders={signed}, Signature={signature}";response=await client.request(method,url,content=payload,headers=headers);response.raise_for_status();return response
        response=await request("GET", "https://dns.myhuaweicloud.com/v2.1/recordsets?name="+quote(hostname+".",safe='')+"&type="+record_type);records=response.json().get("recordsets",[]);record=records[0] if records else None
        if record and record.get("records",[None])[0]==value:return
        payload={"name":hostname+".","type":record_type,"records":[value],"ttl":300}
        if record:url=f"https://dns.myhuaweicloud.com/v2.1/zones/{record['zone_id']}/recordsets/{record['id']}";response=await request("PUT",url,payload)
        else:
            zones=await request("GET","https://dns.myhuaweicloud.com/v2/zones?name="+quote(domain,safe=''));zone=zones.json().get("zones",[])[0];response=await request("POST",f"https://dns.myhuaweicloud.com/v2.1/zones/{zone['id']}/recordsets",payload)
