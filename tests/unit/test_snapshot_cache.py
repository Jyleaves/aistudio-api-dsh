from aistudio_api.infrastructure.cache.snapshot_cache import SnapshotCache


def test_snapshot_cache_isolated_by_account_namespace():
    cache = SnapshotCache(ttl=60, max_size=10)
    cache.set_namespace("account-a")
    cache.put("same prompt", "snap-a", "url-a", {}, "body-a")

    cache.set_namespace("account-b")
    assert cache.get("same prompt") is None

    cache.put("same prompt", "snap-b", "url-b", {}, "body-b")
    cache.set_namespace("account-a")
    assert cache.get("same prompt")[0] == "snap-a"

    cache.set_namespace("account-b")
    assert cache.get("same prompt")[0] == "snap-b"
