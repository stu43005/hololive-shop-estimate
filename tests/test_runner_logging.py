from estimator_king.bot import runner


def test_runner_has_module_logger_with_qualified_name():
    assert runner.logger.name == "estimator_king.bot.runner"
