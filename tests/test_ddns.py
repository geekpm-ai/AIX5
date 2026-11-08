import pytest

from aix5_ai.ddns import DdnsClient, validate_hostname


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class CloudflareClient:
    def __init__(self):
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url.endswith("/zones"):
            return Response({"success": True, "result": [{"id": "zone-1"}] if kwargs["params"]["name"] == "example.com" else []})
        return Response({"success": True, "result": []})

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return Response({"success": True})

    async def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        return Response({"success": True})


class DNSPodClient:
    def __init__(self):
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("Record.List"):
            return Response({"status": {"code": "1", "message": "Running"}, "records": []})
        return Response({"status": {"code": "1", "message": "Operation succeeded"}})


class CountingClient(CloudflareClient):
    pass


def test_hostname_validation_rejects_urls_and_accepts_fqdn():
    assert validate_hostname("Camera.Example.com.") == "camera.example.com"
    with pytest.raises(ValueError):
        validate_hostname("https://camera.example.com")
    with pytest.raises(ValueError):
        validate_hostname("localhost")


@pytest.mark.asyncio
async def test_cloudflare_updates_a_and_aaaa_records_without_uploading_credentials():
    client = CloudflareClient()
    result = await DdnsClient(http_client=client).update(
        {"provider": "cloudflare", "hostname": "camera.example.com", "ipv4": True, "ipv6": True},
        "secret-token",
        addresses={"ipv4": "198.51.100.8", "ipv6": "2001:db8::8"},
    )
    assert result.changed == ["IPV4", "IPV6"]
    assert [method for method, _, _ in client.calls].count("POST") == 2
    assert all(kwargs["headers"]["Authorization"] == "Bearer secret-token" for method, _, kwargs in client.calls if method in {"GET", "POST"})


@pytest.mark.asyncio
async def test_unchanged_addresses_are_checked_but_do_not_call_dns_api():
    client = CountingClient()
    result = await DdnsClient(http_client=client).update(
        {"provider": "cloudflare", "hostname": "camera.example.com", "ipv4": True, "ipv6": True},
        {"access_key_id": "token"},
        addresses={"ipv4": "198.51.100.8", "ipv6": "2001:db8::8"},
        previous={"ipv4": "198.51.100.8", "ipv6": "2001:db8::8"},
    )
    assert result.changed == []
    assert result.unchanged == ["IPV4", "IPV6"]
    assert client.calls == []


@pytest.mark.asyncio
async def test_only_changed_address_type_is_submitted():
    client = CountingClient()
    result = await DdnsClient(http_client=client).update(
        {"provider": "cloudflare", "hostname": "camera.example.com", "ipv4": True, "ipv6": True},
        {"access_key_id": "token"},
        addresses={"ipv4": "198.51.100.9", "ipv6": "2001:db8::8"},
        previous={"ipv4": "198.51.100.8", "ipv6": "2001:db8::8"},
    )
    assert result.changed == ["IPV4"]
    assert result.unchanged == ["IPV6"]
    assert len(client.calls) == 4  # zone discovery, record lookup and write for A only


@pytest.mark.asyncio
async def test_dnspod_uses_id_token_and_updates_each_record_type():
    client = DNSPodClient()
    result = await DdnsClient(http_client=client).update(
        {"provider": "dnspod", "hostname": "camera.example.com", "ipv4": True, "ipv6": False},
        "123,secret",
        addresses={"ipv4": "198.51.100.8", "ipv6": None},
    )
    assert result.changed == ["IPV4"]
    assert client.calls[0][1]["data"]["login_token"] == "123,secret"
    assert client.calls[1][0].endswith("Record.Create")


@pytest.mark.asyncio
async def test_alidns_uses_access_key_signature_and_record_api():
    class AliClient:
        def __init__(self):
            self.calls = []

        async def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            params = kwargs["params"]
            if params["Action"] == "DescribeDomainRecords":
                return Response({"DomainRecords": {"Record": []}})
            return Response({})

    client = AliClient()
    result = await DdnsClient(http_client=client).update(
        {"provider": "alidns", "hostname": "camera.example.com", "ipv4": True, "ipv6": False},
        "access-id,access-secret",
        addresses={"ipv4": "198.51.100.8", "ipv6": None},
    )
    assert result.changed == ["IPV4"]
    assert client.calls[0][1]["params"]["Action"] == "DescribeDomainRecords"
    assert client.calls[0][1]["params"]["AccessKeyId"] == "access-id"
    assert client.calls[0][1]["params"]["Signature"]


def test_provider_catalog_contains_ddns_go_provider_names():
    from aix5_ai.ddns import DDNS_PROVIDER_CATALOG

    for provider in ["alidns", "aliesa", "tencentcloud", "huaweicloud", "baiducloud", "godaddy", "vercel", "cloudns", "desec"]:
        assert provider in DDNS_PROVIDER_CATALOG


def test_supported_catalog_includes_tencent_baidu_huawei():
    from aix5_ai.ddns import SUPPORTED_PROVIDERS

    assert {"tencentcloud", "baiducloud", "huaweicloud"} <= set(SUPPORTED_PROVIDERS)
