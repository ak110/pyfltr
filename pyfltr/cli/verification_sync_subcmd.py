import argparse
import logging
import subprocess

import yaml

import pyfltr.verification_sync

logger = logging.getLogger(__name__)


def execute(_parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    try:
        pyfltr.verification_sync.update(args.update)
    except (ValueError, OSError, subprocess.CalledProcessError, yaml.YAMLError) as e:
        logger.error(f"verification-sync: {e}")
        return 1
    return 0
