# Copyright 2021 Google Inc
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import string
import random
from cli.utils import shared

# Build paths inside the project like this: os.path.join(BASE_DIR, ...)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = (os.getenv('DEBUG', 'False') == 'True')

#Project level configuration
PROJECT = os.environ.get("PROJECT", shared.get_default_stage_name())
REGION = os.environ.get("REGION", "us-central1")

# Database configuration
# Generates a cryptographically secured random password for the database user.
# Source: https://stackoverflow.com/a/23728630
random_password = ''.join(random.SystemRandom().choice(
    string.ascii_lowercase + string.digits) for _ in range(16))

# Load variables if available. Otherwise, default to defaults.
DATABASE_NAME = os.environ.get("DATABASE_NAME", "crmintapp-db")
DATABASE_INSTANCE_NAME = os.environ.get("DATABASE_INSTANCE_NAME", DATABASE_NAME)
DATABASE_USER = os.environ.get("DATABASE_USER", "crmintapp")
DATABASE_PASSWORD = os.environ.get('DATABASE_PASSWORD', random_password)
DATABASE_PUBLIC_IP = (os.getenv('DATABASE_PUBLIC_IP', 'False') == 'True')
DATABASE_BACKUP_ENABLED = (os.getenv('DATABASE_BACKUP_ENABLED', 'False') == 'True')
DATABASE_HA_ENABLED = (os.getenv('DATABASE_HA_ENABLED', 'True') == 'False')
DATABASE_REGION = os.environ.get("DATABASE_REGION", REGION)
DATABASE_TIER = os.environ.get("DATABASE_TIER", "db-g1-small")
DATABASE_PROJECT = os.environ.get("DATABASE_PROJECT", PROJECT)
DATABASE_REGION = os.environ.get("DATABASE_REGION", REGION)

# Networking conf
NETWORK = os.environ.get("NETWORK", "crmint-vpc")
SUBNET = os.environ.get("SUBNET", "crmint-{}-subnet".format(REGION))
SUBNET_CIDR = os.environ.get("SUBNET_CIDR", "10.0.0.0/28")
CONNECTOR = os.environ.get("CONNECTOR", "crmint-vpc-serverless-connector")
CONNECTOR_CIDR = os.environ.get("CONNECTOR_CIDR", "10.0.0.16/28")
NETWORK_SERVICE_PROJECT = os.environ.get("NETWORK_SERVICE_PROJECT", PROJECT)
NETWORK_HOST_PROJECT = os.environ.get("NETWORK_HOST_PROJECT", PROJECT)

#AppEngine config
GAE_PROJECT = os.environ.get("GAE_PROJECT", PROJECT)
GAE_REGION = os.environ.get("GAE_REGION", REGION)
GAE_APP_TITLE = os.environ.get("GAE_APP_TITLE", " ".join(shared.get_default_stage_name().split("-")).title())