from estimator_king.crawler import shopify


def test_shopify_has_module_logger_with_qualified_name():
    assert shopify.logger.name == "estimator_king.crawler.shopify"
