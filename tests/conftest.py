"""Shared test configuration: keep structlog quiet so test output stays readable."""

import logging

import structlog

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
