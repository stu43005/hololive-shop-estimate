from concurrent.futures import ThreadPoolExecutor

from estimator_king.database.repository import ProductState, ProductStateRepository


def _state(i: int) -> ProductState:
    return ProductState(
        external_key=f"s:{i}", store_id="s", product_id=str(i),
        product_url=f"https://x/p/{i}", content_hash="h", normalizer_version=2,
    )


def test_concurrent_upserts_all_persist(tmp_path):
    db = str(tmp_path / "state.db")
    with ProductStateRepository(db) as repo:
        errors: list[str] = []

        def worker(i: int) -> None:
            try:
                repo.upsert(_state(i))
            except Exception as exc:  # noqa: BLE001
                errors.append(type(exc).__name__)

        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(worker, range(60)))

        assert errors == []
        count = repo.connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        assert count == 60
