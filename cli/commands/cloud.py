# Copyright 2018 Google Inc
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
import subprocess
import signal
from glob import glob
from io import StringIO
from yaml import safe_load, safe_dump

import click

from cli.commands import stages
from cli.utils import constants
from cli.utils import shared
from cli.utils import settings

def fetch_stage_or_default(stage_name=None, debug=False):
  if not stage_name:
    stage_name = shared.get_default_stage_name(debug=debug)

  if not shared.check_stage_file(stage_name):
    click.echo(click.style("Stage file '%s' not found." % stage_name, fg='red', bold=True))
    return None

  stage = shared.get_stage_object(stage_name)
  return stage_name, stage


@click.group()
def cli():
  """Manage your CRMint instance on GCP."""
  pass


####################### SETUP #######################
'''
Notes
setup requires refactoring to handle infra in a declarative way
Might be good to move to tearraform and handle state via GCS
At the least, it should be refactored to migrate creation to a shared component
'''

def _check_if_appengine_instance_exists(stage, debug=False):
  gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"
  command = "{gcloud_bin} app describe --verbosity critical --project={project_id} | grep -q 'codeBucket'".format(
      gcloud_bin=gcloud_command,
      project_id=stage.project_id)
  status, out, err = shared.execute_command("Check if App Engine already exists",
      command,
      report_empty_err=False,
      debug=debug)
  return status == 0


def create_appengine(stage, debug=False):
  if _check_if_appengine_instance_exists(stage, debug=debug):
    click.echo("     App Engine already exists.")
    return

  gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"
  command = "{gcloud_bin} app create --project={gae_project} --region={gae_region}".format(
      gcloud_bin=gcloud_command,
      gae_project=stage.gae_project,
      gae_region=stage.gae_region)
  shared.execute_command("Create the App Engine instance", command, debug=debug)


def _check_if_vpc_exists(stage, debug=False):
  gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"
  command = "{gcloud_bin} compute networks describe {network} --verbosity critical --project={network_project}".format(
      gcloud_bin=gcloud_command,
      network=stage.network,
      network_project=stage.network_project)
  status, out, err = shared.execute_command("Check if VPC already exists",
      command,
      report_empty_err=False,
      debug=debug)
  return status == 0

def _check_if_peering_exists(stage, debug=False):
  gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"
  command = "{gcloud_bin} services vpc-peerings list --network={network} --verbosity critical --project={network_project} | grep {network}-psc".format(
      gcloud_bin=gcloud_command,
      network=stage.network,
      network_project=stage.network_project)
  status, out, err = shared.execute_command("Check if VPC Peering exists",
      command,
      report_empty_err=False,
      debug=debug)
  return status == 0

def _check_if_firewall_rules_exist(stage, rule_name, debug=False):
  gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"
  command = "{gcloud_bin} compute firewall-rules describe {rule_name} --verbosity critical --project={network_project} | grep {rule_name}".format(
      gcloud_bin=gcloud_command,
      rule_name=rule_name,
      network_project=stage.network_project)
  status, out, err = shared.execute_command("Check if VPC Peering exists",
      command,
      report_empty_err=False,
      debug=debug)
  return status == 0

def create_firewall_rules(stage, debug=False):
  '''
  Creates 3 required firewall rules needed for App Engine to VPC connection
  These are now handled automatically by GCP so not required.
  This function is not invoked but leaving in case required.
  '''
  gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"
  rules = [
    "{gcloud_bin} compute firewall-rules create serverless-to-vpc-connector \
    --allow tcp:667,udp:665-666,icmp \
    --source-ranges 107.178.230.64/26,35.199.224.0/19 \
    --direction=INGRESS \
    --target-tags vpc-connector \
    --network={network}".format(
      gcloud_bin=gcloud_command,
      network=stage.network
    ),
    "{gcloud_bin} compute firewall-rules create vpc-connector-to-serverless \
    --allow tcp:667,udp:665-666,icmp \
    --destination-ranges 107.178.230.64/26,35.199.224.0/19 \
    --direction=EGRESS \
    --target-tags vpc-connector \
    --network={network}".format(
      gcloud_bin=gcloud_command,
      network=stage.network
    ),
    "{gcloud_bin}  compute firewall-rules create vpc-connector-health-checks \
    --allow tcp:667 \
    --source-ranges 130.211.0.0/22,35.191.0.0/16,108.170.220.0/23 \
    --direction=INGRESS \
    --target-tags vpc-connector \
    --network={network}".format(
      gcloud_bin=gcloud_command,
      network=stage.network
    )
  ]

  for rule_command in rules:
    rule_name = rule_command.split()[rule_command.split().index("create") + 1]
    if _check_if_firewall_rules_exist(stage, rule_name=rule_name):
      continue
    else:
      shared.execute_command("Creating Firewall rule {}".format(rule_name), rule_command, debug=debug)

def create_vpc(stage, debug=False):
  '''
  Creates a VPC in the project. Then allocates an IP Range for cloudSQL.
  finally, create peering to allow cloudSQL connection via private service access.
  To do:
  - Add support for shared VPC logic
  - Manage XPN Host permissions or add pre-requisite for shared vpc
  '''
  if _check_if_vpc_exists(stage, debug=debug):
    click.echo("     VPC already exists.")
    return

  gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"
  command = "{gcloud_bin} compute networks create {network} --project={network_project} \
    --subnet-mode=custom \
    --bgp-routing-mode=regional \
    --mtu=1460".format(
      gcloud_bin=gcloud_command,
      network=stage.network,
      network_project=stage.network_project
    )
  shared.execute_command("Create the VPC", command, debug=debug)

  command = "{gcloud_bin} compute addresses create {network}-psc \
      --global \
      --purpose=VPC_PEERING \
      --addresses=192.168.0.0 \
      --prefix-length=24 \
      --network={network}".format(
      gcloud_bin=gcloud_command,
      network=stage.network,
      network_project=stage.network_project
    )
  shared.execute_command("Allocating an IP address range", command, debug=debug)

  if _check_if_peering_exists(stage, debug=debug):
    command = "{gcloud_bin} services vpc-peerings update \
      --service=servicenetworking.googleapis.com \
      --ranges={network}-psc \
      --network={network} \
      --force \
      --project={network_project}".format(
      gcloud_bin=gcloud_command,
      network=stage.network,
      network_project=stage.network_project
    )
  else:
    command = "{gcloud_bin} services vpc-peerings connect \
        --service=servicenetworking.googleapis.com \
        --ranges={network}-psc \
        --network={network} \
        --project={network_project}".format(
        gcloud_bin=gcloud_command,
        network=stage.network,
        network_project=stage.network_project
      )
  shared.execute_command("Creating or updating the private connection", command, debug=debug)

def _check_if_subnet_exists(stage, debug=False):
  # Check that subnet exist in service project.
  gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"
  command = "{gcloud_bin} compute networks subnets describe {subnet} --verbosity critical --project={network_project} \
    --region={subnet_region}".format(
      gcloud_bin=gcloud_command,
      subnet=stage.subnet,
      subnet_region=stage.subnet_region,
      network_project=stage.network_project)
  status, out, err = shared.execute_command("Check if VPC Subnet already exists",
      command,
      report_empty_err=False,
      debug=debug)
  return status == 0


def _check_if_connector_subnet_exists(stage, debug=False):
  # Check that subnet exist in service project.
  gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"
  command = "{gcloud_bin} compute networks subnets describe {connector_subnet} --verbosity critical --project={network_project} \
    --region={subnet_region}".format(
      gcloud_bin=gcloud_command,
      connector_subnet=stage.connector_subnet,
      subnet_region=stage.subnet_region,
      network_project=stage.network_project)
  status, out, err = shared.execute_command("Check if VPC Subnet already exists",
      command,
      report_empty_err=False,
      debug=debug)
  return status == 0

def create_subnet(stage, debug=False):
  if _check_if_subnet_exists(stage, debug=debug):
    click.echo("     VPC App Subnet already exists.")
    pass
  else:
    gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"
    command_subnet = "{gcloud_bin} compute networks subnets create {subnet} \
      --network={network} \
      --range={subnet_cidr} \
      --region={subnet_region} \
      --project={network_project}".format(
        gcloud_bin=gcloud_command,
        subnet=stage.subnet,
        network=stage.network,
        subnet_cidr=stage.subnet_cidr,
        subnet_region=stage.subnet_region,
        network_project=stage.network_project
      )

    shared.execute_command("Create the VPC App Subnet", command_subnet, debug=debug)

  if _check_if_connector_subnet_exists(stage, debug=debug):
    click.echo("     VPC Connector Subnet already exists.")
    pass
  else:
    gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"
    command_connector_subnet = "{gcloud_bin} compute networks subnets create {connector_subnet} \
      --network={network} \
      --range={connector_cidr} \
      --region={subnet_region} \
      --project={network_project}".format(
        gcloud_bin=gcloud_command,
        connector_subnet=stage.connector_subnet,
        network=stage.network,
        connector_cidr=stage.connector_cidr,
        subnet_region=stage.subnet_region,
        network_project=stage.network_project
      )
    shared.execute_command("Create the VPC Connector Subnet", command_connector_subnet, debug=debug)
  return

def _check_if_vpc_connector_exists(stage, debug=False):
  gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"
  command = "{gcloud_bin} compute networks vpc-access connectors describe {connector} --verbosity critical --project={network_project} | grep {connector}".format(
      gcloud_bin=gcloud_command,
      connector=stage.connector,
      network_project=stage.network_project)
  status, out, err = shared.execute_command("Check if VPC Connector already exists",
      command,
      report_empty_err=False,
      debug=debug)
  return status == 0


def create_vpc_connector(stage, debug=False):
  '''
  Creates a VPC in the project.
  To do:
  - Add support for shared VPC logic
  - Add pre-requisite for shared vpc (XPN Host permissions)
  '''
  if _check_if_vpc_connector_exists(stage, debug=debug):
    click.echo("     VPC Connector already exists.")
  else:
    gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"
    command = "{gcloud_bin} compute networks vpc-access connectors create {connector} \
        --region {subnet_region} \
        --subnet {connector_subnet} \
        --subnet-project {network_project} \
        --min-instances {connector_min_instances} \
        --max-instances {connector_max_instances} \
        --machine-type {connector_machine_type}".format(
      gcloud_bin=gcloud_command,
      connector=stage.connector,
      subnet_region=stage.subnet_region,
      connector_subnet=stage.connector_subnet,
      network_project=stage.network_project,
      connector_min_instances=stage.connector_min_instances,
      connector_max_instances=stage.connector_max_instances,
      connector_machine_type=stage.connector_machine_type
      )
    shared.execute_command("Create the VPC Connector", command, debug=debug)
  return

def create_service_account_key_if_needed(stage, debug=False):
  if shared.check_service_account_file(stage):
    click.echo("     Service account key already exists.")
    return

  service_account_file = shared.get_service_account_file(stage)
  gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"
  command = "{gcloud_bin} iam service-accounts keys create \"{service_account_file}\" \
    --iam-account=\"{project_id}@appspot.gserviceaccount.com\" \
    --key-file-type='json' \
    --project={project_id}".format(
      gcloud_bin=gcloud_command,
      project_id=stage.project_id,
      service_account_file=service_account_file)
  shared.execute_command("Create the service account key", command, debug=debug)


def confirm_authorized_user_owner_role(stage, debug=False):
  '''
  Checks that the user running the deployment has owner permissions on the 
  target deployment project_id.

  TO DO:
  Move away from primitive role (owner). Either pre-defined roles or custome
  role will be required to handle in best practices.
  '''
  gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"
  auth_list_command = "{gcloud_bin} auth list \
    --format=\"value(account)\"".format(
    gcloud_bin=gcloud_command)
  status, out, err = shared.execute_command(
    "Getting authorized user", auth_list_command, debug=debug)
  command = "{gcloud_bin} projects get-iam-policy {project_id} \
    --flatten=\"bindings[].members\" \
    --format=\"table(bindings.role)\" \
    --filter=\"bindings.members:{auth_user}\" \
    | grep -q 'roles/owner'".format(
      gcloud_bin=gcloud_command,
      auth_user=out.strip(),
      project_id=stage.project_id)
  status, out, err = shared.execute_command("Check if authorized user is owner",
      command,
      report_empty_err=False,
      debug=debug)
  return status == 0


def downgrade_app_engine_python(stage, debug=False):
  # https://issuetracker.google.com/202171426
  command = "sudo apt-get -y install google-cloud-sdk-app-engine-python=359.0.0-0 --allow-downgrades"
  shared.execute_command("Downgrade app-engine-python", command, debug=debug)
  
  
def grant_required_permissions(stage, debug=False):
  gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"
  project_number_command = "{gcloud_bin} projects list \
    --filter=\"{project_id}\" \
    --format=\"value(PROJECT_NUMBER)\"".format(
      gcloud_bin=gcloud_command,
      project_id=stage.project_id)
  status, project_number, err = shared.execute_command(
      "Getting the project number", project_number_command, debug=debug)
  
  commands = [
    "{gcloud_bin} projects add-iam-policy-binding {project_id} \
    --member=\"serviceAccount:{project_number}@cloudbuild.gserviceaccount.com\" \
    --role=\"roles/storage.objectViewer\"".format(
      gcloud_bin=gcloud_command,
      project_id=stage.project_id,
      project_number=project_number.strip()),
    "{gcloud_bin} projects add-iam-policy-binding {project_id} \
    --role \"roles/compute.networkUser\" \
    --member \"serviceAccount:service-{project_number}@gcp-sa-vpcaccess.iam.gserviceaccount.com\"".format(
      gcloud_bin=gcloud_command,
      project_id=stage.project_id,
      project_number=project_number.strip()),
    "{gcloud_bin} projects add-iam-policy-binding {project_id} \
    --role \"roles/compute.networkUser\" \
    --member \"serviceAccount:service-{project_number}@cloudservices.gserviceaccount.com\"".format(
      gcloud_bin=gcloud_command,
      project_id=stage.project_id,
      project_number=project_number.strip())
  ]

  total = len(commands)
  idx = 1
  for cmd in commands:
    shared.execute_command("Grant required permissions (%d/%d)" % (idx, total),
        cmd,
        debug=debug)
    idx += 1

def _check_if_mysql_instance_exists(stage, debug=False):
  gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"
  command = "{gcloud_bin} sql instances describe --verbosity critical \
    --project={database_project} {database_instance_name} \
    | grep -q '{database_instance_name}'".format(
      gcloud_bin=gcloud_command,
      database_project=stage.database_project,
      database_instance_name=stage.database_instance_name)
  status, out, err = shared.execute_command("Check if MySQL instance already exists",
      command,
      report_empty_err=False,
      debug=debug)
  return status == 0


def create_mysql_instance_if_needed(stage, debug=False):
  if _check_if_mysql_instance_exists(stage, debug=debug):
    click.echo("     MySQL instance already exists.")
    return

  gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"
  command = "{gcloud_bin} beta sql instances create {database_instance_name} \
    --tier={database_tier} --region={database_region} \
    --project={database_project} --database-version MYSQL_5_7 \
    --storage-auto-increase \
    --network=projects/{network_project}/global/networks/{network} \
    --availability-type={database_ha_type} \
    --authorized-networks={subnet_cidr} \
    --no-assign-ip ".format(
      gcloud_bin=gcloud_command,
      database_instance_name=stage.database_instance_name,
      database_project=stage.database_project,
      database_region=stage.database_region,
      database_tier=stage.database_tier,
      network_project=stage.network_project,
      network=stage.network,
      subnet_cidr=stage.subnet_cidr,
      database_ha_type=stage.database_ha_type
    )
  shared.execute_command("Creating MySQL instance", command, debug=debug)


def _check_if_mysql_user_exists(stage, debug=False):
  gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"
  command = "{gcloud_bin} sql users list \
    --project={database_project} \
    --instance={database_instance_name} \
    | grep -q '{database_username}'".format(
      gcloud_bin=gcloud_command,
      database_project=stage.database_project,
      database_instance_name=stage.database_instance_name,
      database_username=stage.database_username)
  status, out, err = shared.execute_command("Check if MySQL user already exists",
      command,
      report_empty_err=False,
      debug=debug)
  return status == 0


def create_mysql_user_if_needed(stage, debug=False):
  gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"
  if _check_if_mysql_user_exists(stage, debug=debug):
    click.echo("     MySQL user already exists.")
    sql_users_command = "set-password"
    message = "Setting MySQL user's password"
  else:
    sql_users_command = "create"
    message = "Creating MySQL user"
  command = "{gcloud_bin} sql users {sql_users_command} {database_username} \
    --host % \
    --instance={database_instance_name} \
    --password={database_password} \
    --project={project_id}".format(
      gcloud_bin=gcloud_command,
      sql_users_command=sql_users_command,
      project_id=stage.project_id,
      database_instance_name=stage.database_instance_name,
      database_username=stage.database_username,
      database_password=stage.database_password)
  shared.execute_command(message, command, debug=debug)


def _check_if_mysql_database_exists(stage, debug=False):
  gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"
  command = "{gcloud_bin} sql databases list \
    --project={project_id} \
    --instance={database_instance_name} \
    | grep -q '{database_name}'".format(
      gcloud_bin=gcloud_command,
      project_id=stage.project_id,
      database_instance_name=stage.database_instance_name,
      database_name=stage.database_name)
  status, out, err = shared.execute_command("Check if MySQL database already exists",
      command,
      report_empty_err=False,
      debug=debug)
  return status == 0


def create_mysql_database_if_needed(stage, debug=False):
  if _check_if_mysql_database_exists(stage, debug=debug):
    click.echo("     MySQL database already exists.")
    return

  gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"
  command = "{gcloud_bin} sql databases create {database_name} \
    --instance={database_instance_name} \
    --project={project_id}".format(
      gcloud_bin=gcloud_command,
      project_id=stage.project_id,
      database_instance_name=stage.database_instance_name,
      database_name=stage.database_name)
  shared.execute_command("Creating MySQL database", command, debug=debug)


def activate_services(stage, debug=False):
  gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"
  command = "{gcloud_bin} services enable \
    --project={project_id} \
    analytics.googleapis.com \
    analyticsreporting.googleapis.com \
    bigquery-json.googleapis.com \
    cloudapis.googleapis.com \
    logging.googleapis.com \
    storage-api.googleapis.com \
    storage-component.googleapis.com \
    sqladmin.googleapis.com \
    cloudscheduler.googleapis.com \
    cloudbuild.googleapis.com \
    servicenetworking.googleapis.com \
    compute.googleapis.com \
    vpcaccess.googleapis.com \
    dns.googleapis.com \
    appengine.googleapis.com".format(
      gcloud_bin=gcloud_command,
      project_id=stage.project_id)
  shared.execute_command("Activate services", command, debug=debug)


def download_config_files(stage, debug=False):
  stage_file_path = shared.get_stage_file(stage.stage_name)
  service_account_file_path = shared.get_service_account_file(stage)
  command = "cloudshell download-files \
    \"{stage_file}\" \
    \"{service_account_file}\"".format(
      stage_file=stage_file_path,
      service_account_file=service_account_file_path)
  shared.execute_command("Download configuration files", command, debug=debug)


####################### DEPLOY #######################


def install_required_packages(stage, debug=False):
  commands = [
      "mkdir -p ~/.cloudshell",
      "> ~/.cloudshell/no-apt-get-warning",
      "sudo apt-get install -y rsync libmysqlclient-dev",
  ]
  total = len(commands)
  idx = 1
  for cmd in commands:
    shared.execute_command("Install required packages (%d/%d)" % (idx, total),
        cmd,
        debug=debug)
    idx += 1


def display_workdir(stage, debug=False):
  click.echo("     Working directory: %s" % stage.workdir)


def copy_src_to_workdir(stage, debug=False):
  copy_src_cmd = "rsync -r --delete \
    --exclude=.git \
    --exclude=.idea \
    --exclude='*.pyc' \
    --exclude=frontend/node_modules \
    --exclude=backends/data/*.json . {workdir}".format(
      workdir=stage.workdir)

  copy_insight_config_cmd = "cp backends/data/insight.json {workdir}/backends/data/insight.json".format(
      workdir=stage.workdir)

  copy_service_account_cmd = "cp backends/data/{service_account_filename} {workdir}/backends/data/service-account.json".format(
      workdir=stage.workdir,
      service_account_filename=stage.service_account_file)

  copy_db_conf = "echo \'SQLALCHEMY_DATABASE_URI=\"{cloud_db_uri}\"\' > {workdir}/backends/instance/config.py".format(
      workdir=stage.workdir,
      cloud_db_uri=stage.cloud_db_uri)

  copy_app_data = """
cat > %(workdir)s/backends/data/app.json <<EOL
{
  "notification_sender_email": "%(notification_sender_email)s",
  "app_title": "%(app_title)s"
}
EOL""".strip() % dict(
    workdir=stage.workdir,
    app_title=stage.gae_app_title,
    notification_sender_email=stage.notification_sender_email)

  # We dont't use prod environment for the frontend to speed up deploy.
  copy_prod_env = """
cat > %(workdir)s/frontend/src/environments/environment.ts <<EOL
export const environment = {
  production: true,
  app_title: "%(app_title)s",
  enabled_stages: %(enabled_stages)s
}
EOL""".strip() % dict(
    workdir=stage.workdir,
    app_title=stage.gae_app_title,
    enabled_stages="true" if stage.enabled_stages else "false")

  commands = [
      copy_src_cmd,
      copy_insight_config_cmd,
      copy_service_account_cmd,
      copy_db_conf,
      copy_app_data,
      copy_prod_env,
  ]
  total = len(commands)
  idx = 1
  for cmd in commands:
    shared.execute_command("Copy source code to working directory (%d/%d)" % (idx, total),
        cmd,
        cwd=constants.PROJECT_DIR,
        debug=debug)
    idx += 1


def deploy_frontend(stage, debug=False):
  gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"

  frontend_files = ['gae.yaml']

  # Connector object with required configurations
  connector_config = {
    "vpc_access_connector": {
    "name": "projects/{project}/locations/{region}/connectors/{connector}".format(
        project=stage.gae_project,
        region=stage.gae_region,
        connector=stage.connector
      )
      }
    }
  
  # NB: Limit the node process memory usage to avoid overloading
  #     the Cloud Shell VM memory which makes it unresponsive.
  commands = [
      "npm install --legacy-peer-deps",
      "node --max-old-space-size=512 ./node_modules/@angular/cli/bin/ng build",
      "{gcloud_bin} --project={project_id} app deploy {file} --version=v1".format(
          gcloud_bin=gcloud_command,
          file=frontend_files[0],
          project_id=stage.project_id)
  ]
  cmd_workdir = os.path.join(stage.workdir, 'frontend')
  # insert connector config to GAE YAML
  for f in frontend_files:
    try: 
      with open(os.path.join(cmd_workdir, f),'r') as yaml_read:
        r = safe_load(yaml_read)
        r.update(connector_config)

      with open(os.path.join(cmd_workdir, f),'w') as yaml_write:
          safe_dump(r, yaml_write)
    except:
      click.echo(click.style("Unable to insert VPC connector config to App Engine {file}".format(file=f), fg='red'))
      exit(1)

  total = len(commands)
  idx = 1
  for cmd in commands:
    shared.execute_command("Deploy frontend service (%d/%d)" % (idx, total),
        cmd,
        cwd=cmd_workdir,
        debug=debug)
    idx += 1


def deploy_dispatch_rules(stage, debug=False):
  gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"
  # NB: Limit the node process memory usage to avoid overloading
  #     the Cloud Shell VM memory which makes it unresponsive.
  command = "{gcloud_bin} --project={project_id} app deploy dispatch.yaml".format(
      gcloud_bin=gcloud_command,
      project_id=stage.gae_project)
  cmd_workdir = os.path.join(stage.workdir, 'frontend')
  shared.execute_command("Deploy the dispatch.yaml rules",
      command,
      cwd=cmd_workdir,
      debug=debug)


def install_backends_dependencies(stage, debug=False):
  commands = [
      # HACK: fix missing MySQL header for compilation
      "sudo wget https://raw.githubusercontent.com/paulfitz/mysql-connector-c/master/include/my_config.h -P /usr/include/mysql/",
      # Install dependencies in virtualenv
      "virtualenv --python=python2 env",
      "mkdir -p lib",
      "pip install -r ibackend/requirements.txt -t lib",
      "pip install -r jbackend/requirements.txt -t lib",
      # Applying patches requered in GAE environment (alas!).
      "cp -r \"%(patches_dir)s\"/lib/* lib/" % dict(patches_dir=constants.PATCHES_DIR),
      "find \"%(workdir)s\" -name '*.pyc' -exec rm {} \;" % dict(workdir=stage.workdir),
  ]
  cmd_workdir = os.path.join(stage.workdir, 'backends')
  total = len(commands)
  idx = 1
  for cmd in commands:
    shared.execute_command("Install backends dependencies (%d/%d)" % (idx, total),
        cmd,
        cwd=cmd_workdir,
        debug=debug)
    idx += 1


def deploy_backends(stage, debug=False):
  gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"

  backend_files = ['gae_ibackend.yaml', 'gae_jbackend.yaml', 'cron.yaml']

  # Connector object with required configurations
  connector_config = {
    "vpc_access_connector": {
      "name": "projects/{project}/locations/{region}/connectors/{connector}".format(
        project=stage.gae_project,
        region=stage.gae_region,
        connector=stage.connector
      )
      }
    }

  commands = [
      ". env/bin/activate && {gcloud_bin} --project={project_id} app deploy {file} --version=v1".format(
          gcloud_bin=gcloud_command,
          file=backend_files[0],
          project_id=stage.gae_project),
      ". env/bin/activate && {gcloud_bin} --project={project_id} app deploy {file} --version=v1".format(
          gcloud_bin=gcloud_command,
          file=backend_files[1],
          project_id=stage.gae_project),
      ". env/bin/activate && {gcloud_bin} --project={project_id} app deploy {file}".format(
          gcloud_bin=gcloud_command,
          file=backend_files[2],
          project_id=stage.gae_project)
  ]

  cmd_workdir = os.path.join(stage.workdir, 'backends')

  # insert connector config to GAE YAML
  for f in backend_files:
    if f is 'cron.yaml':
      continue
    try: 
      with open(os.path.join(cmd_workdir, f),'r') as yaml_read:
        r = safe_load(yaml_read)
        r.update(connector_config)

      with open(os.path.join(cmd_workdir, f),'w') as yaml_write:
          safe_dump(r, yaml_write)
    except:
      click.echo(click.style("Unable to insert VPC connector config to App Engine {file}".format(file=f), fg='red'))
      exit(1)

  total = len(commands)
  idx = 1
  for cmd in commands:
    shared.execute_command("Deploy backend services (%d/%d)" % (idx, total),
        cmd,
        cwd=cmd_workdir,
        debug=debug)
    idx += 1


def download_cloud_sql_proxy(stage, debug=False):
  cloud_sql_proxy_path = "/usr/bin/cloud_sql_proxy"
  if os.path.isfile(cloud_sql_proxy_path):
    os.environ["CLOUD_SQL_PROXY"] = cloud_sql_proxy_path
  else:
    cloud_sql_proxy_path = "{}/bin/cloud_sql_proxy".format(os.environ["HOME"])
    if not os.path.isfile(cloud_sql_proxy_path):
      if not os.path.exists(os.path.dirname(cloud_sql_proxy_path)):
        os.mkdir(os.path.dirname(cloud_sql_proxy_path), 0755)
      cloud_sql_download_link = "https://dl.google.com/cloudsql/cloud_sql_proxy.linux.amd64"
      download_command = "curl -L {} -o {}".format(cloud_sql_download_link,
                                                   cloud_sql_proxy_path)
      shared.execute_command("Downloading Cloud SQL proxy", download_command,
          debug=debug)
    os.environ["CLOUD_SQL_PROXY"] = cloud_sql_proxy_path


def start_cloud_sql_proxy(stage, debug=False):
  gcloud_command = "$GOOGLE_CLOUD_SDK/bin/gcloud --quiet"
  commands = [
      (
          "mkdir -p {cloudsql_dir}".format(cloudsql_dir=stage.cloudsql_dir),
          False,
      ),
      (
          "echo \"CLOUD_SQL_PROXY=$CLOUD_SQL_PROXY\"",
          False,
      ),
      (
          "$CLOUD_SQL_PROXY -projects={project_id} -instances={database_instance_conn_name}=tcp:3306 -dir={cloudsql_dir} 2>/dev/null &".format(
              project_id=stage.database_project,
              cloudsql_dir=stage.cloudsql_dir,
              database_instance_conn_name=stage.database_instance_conn_name),
          True,
      ),
      (
          "sleep 5",  # Wait for cloud_sql_proxy to start.
          False
      ),
  ]
  total = len(commands)
  idx = 1
  for comp in commands:
    cmd, force_std_out = comp
    shared.execute_command("Start CloudSQL proxy (%d/%d)" % (idx, total),
        cmd,
        cwd='.',
        force_std_out=force_std_out,
        debug=debug)
    idx += 1


def stop_cloud_sql_proxy(stage, debug=False):
  command = "kill -9 $(ps | grep cloud_sql_proxy | awk '{print $1}')"
  shared.execute_command("Stop CloudSQL proxy",
      command,
      cwd='.',
      debug=debug)


def prepare_flask_envars(stage, debug=False):
  os.environ["PYTHONPATH"] = "{google_sdk_dir}/platform/google_appengine:lib".format(
      google_sdk_dir=os.environ["GOOGLE_CLOUD_SDK"])
  os.environ["FLASK_APP"] = "run_ibackend.py"
  os.environ["FLASK_DEBUG"] = "1"
  os.environ["APPLICATION_ID"] = stage.project_id

  # Use the local Cloud SQL Proxy url
  command = "echo \'SQLALCHEMY_DATABASE_URI=\"{cloud_db_uri}\"\' > {workdir}/backends/instance/config.py".format(
      workdir=stage.workdir,
      cloud_db_uri=stage.local_db_uri)
  shared.execute_command("Configure Cloud SQL proxy settings",
      command,
      cwd='.',
      debug=debug)


def _run_flask_command(stage, step_name, flask_command_name="--help", debug=False):
  cmd_workdir = os.path.join(stage.workdir, 'backends')
  command = ". env/bin/activate && python -m flask {command_name}".format(
      command_name=flask_command_name)
  shared.execute_command(step_name,
      command,
      cwd=cmd_workdir,
      debug=debug)


def run_flask_db_upgrade(stage, debug=False):
  _run_flask_command(stage, "Applying database migrations",
      flask_command_name="db upgrade", debug=debug)


def run_flask_db_seeds(stage, debug=False):
  _run_flask_command(stage, "Sowing DB seeds",
      flask_command_name="db-seeds", debug=debug)


####################### RESET #######################


def run_reset_pipelines(stage, debug=False):
  _run_flask_command(stage, "Reset statuses of jobs and pipelines",
      flask_command_name="reset-pipelines", debug=debug)


####################### SUB-COMMANDS #################


@cli.command('setup')
@click.option('--stage_name', type=str, default=None)
@click.option('--debug/--no-debug', default=False)
def setup(stage_name, debug):
  """Setup the GCP environment for deploying CRMint."""
  click.echo(click.style(">>>> Setup", fg='magenta', bold=True))

  stage_name, stage = fetch_stage_or_default(stage_name, debug=debug)
  if stage is None:
    exit(1)

  # Enriches stage with other variables.
  stage = shared.before_hook(stage, stage_name)

  # Runs setup steps.
  components = [
      downgrade_app_engine_python,
      activate_services,
      create_vpc,
      create_subnet,
      create_vpc_connector,
      create_appengine,
      create_service_account_key_if_needed,
      grant_required_permissions,
      create_mysql_instance_if_needed,
      create_mysql_user_if_needed,
      create_mysql_database_if_needed,
      download_config_files,
  ]
  if confirm_authorized_user_owner_role(stage, debug=debug):
    click.echo("     Authorized user confirmed as owner.")
    for component in components:
      component(stage, debug=debug)
  else:
    click.echo(click.style("""     This user doesn't have the owner role. 
     Only owners can deploy this application.
     Exiting setup.""", fg='red', bold=True))
    exit(1)
  click.echo(click.style("Done.", fg='magenta', bold=True))


def _setup(stage_name, debug):
  """Setup the GCP environment for deploying CRMint."""
  stage_name, stage = fetch_stage_or_default(stage_name, debug=debug)
  if stage is None:
    click.echo(click.style("Fix that issue by running: $ crmint stages create", fg='green'))
    exit(1)

  # Enriches stage with other variables.
  stage = shared.before_hook(stage, stage_name)

  # Runs setup steps.
  components = [
      downgrade_app_engine_python,
      activate_services,
      create_appengine,
      create_service_account_key_if_needed,
      grant_cloud_build_permissions,
      create_mysql_instance_if_needed,
      create_mysql_user_if_needed,
      create_mysql_database_if_needed,
      download_config_files,
  ]
  if confirm_authorized_user_owner_role(stage, debug=debug):
    click.echo("     Authorized user confirmed as owner.")
    for component in components:
      component(stage, debug=debug)
  else:
    click.echo(click.style("""     This user doesn't have the owner role. 
     Only owners can deploy this application.
     Exiting setup.""", fg='red', bold=True))
    exit(1)
  click.echo(click.style("Done.", fg='magenta', bold=True))


@cli.command('deploy')
@click.option('--stage_name', type=str, default=None)
@click.option('--debug/--no-debug', default=False)
@click.option('--skip-deploy-backends', is_flag=True, default=False)
@click.option('--skip-deploy-frontend', is_flag=True, default=False)
def deploy(stage_name, debug, skip_deploy_backends, skip_deploy_frontend):
  """Deploy CRMint on GCP."""
  click.echo(click.style(">>>> Deploy", fg='magenta', bold=True))

  stage_name, stage = fetch_stage_or_default(stage_name, debug=debug)
  if stage is None:
    click.echo(click.style("Fix that issue by running: $ crmint cloud setup", fg='green'))
    exit(1)

  # Enriches stage with other variables.
  stage = shared.before_hook(stage, stage_name)

  # Runs deploy steps.
  components = [
      install_required_packages,
      display_workdir,
      copy_src_to_workdir,
      install_backends_dependencies,
      deploy_frontend,
      deploy_backends,
      deploy_dispatch_rules,
      download_cloud_sql_proxy,
      start_cloud_sql_proxy,
      prepare_flask_envars,
      run_flask_db_upgrade,
      run_flask_db_seeds,
      stop_cloud_sql_proxy,
  ]

  if skip_deploy_backends and (deploy_backends in components):
    components.remove(deploy_backends)
  if skip_deploy_frontend and (deploy_frontend in components):
    components.remove(deploy_frontend)

  for component in components:
    component(stage, debug=debug)
  click.echo(click.style("Done.", fg='magenta', bold=True))


def _deploy(stage_name, debug):
  """Deploy CRMint on GCP."""
  stage_name, stage = fetch_stage_or_default(stage_name, debug=debug)
  if stage is None:
    click.echo(click.style("Fix that issue by running: $ crmint cloud setup", fg='green'))
    exit(1)

  # Enriches stage with other variables.
  stage = shared.before_hook(stage, stage_name)

  # Runs deploy steps.
  components = [
      install_required_packages,
      display_workdir,
      copy_src_to_workdir,
      install_backends_dependencies,
      deploy_frontend,
      deploy_backends,
      deploy_dispatch_rules,
      download_cloud_sql_proxy,
      start_cloud_sql_proxy,
      prepare_flask_envars,
      run_flask_db_upgrade,
      run_flask_db_seeds,
      stop_cloud_sql_proxy,
  ]

  for component in components:
    component(stage, debug=debug)
  click.echo(click.style("Done.", fg='magenta', bold=True))


@cli.command('reset')
@click.option('--stage_name', type=str, default=None)
@click.option('--debug/--no-debug', default=False)
def reset(stage_name, debug):
  """Reset pipeline statuses."""
  click.echo(click.style(">>>> Reset pipelines", fg='magenta', bold=True))

  stage_name, stage = fetch_stage_or_default(stage_name, debug=debug)
  if stage is None:
    click.echo(click.style("Fix that issue by running: `$ crmint cloud setup`", fg='green'))
    exit(1)

  # Enriches stage with other variables.
  stage = shared.before_hook(stage, stage_name)

  # Runs setup stages.
  components = [
      install_required_packages,
      display_workdir,
      copy_src_to_workdir,
      install_backends_dependencies,
      download_cloud_sql_proxy,
      start_cloud_sql_proxy,
      prepare_flask_envars,
      run_reset_pipelines,
      stop_cloud_sql_proxy,
  ]
  for component in components:
    component(stage, debug=debug)
  click.echo(click.style("Done.", fg='magenta', bold=True))


@cli.command('begin')
@click.option('--stage_name', type=str, default=None)
@click.option('--debug/--no-debug', default=False)
def begin(stage_name, debug):
  """Combined steps to deploy CRMint."""
  click.echo(click.style(">>>> Starting", fg='magenta', bold=True))

  stages._create(stage_name)
  _setup(stage_name, debug)
  _deploy(stage_name, debug)


if __name__ == '__main__':
  cli()
