# Phase 1a: the public hello image, so Terraform is proven before any app of
# yours is in the picture. Phase 1b swaps `image` for your own build.
#
# From Phase 3 onward the agent writes files exactly like this one - it never
# writes Terraform, only values that the module's own validation rules accept.

project_id = "agentic-poc-5b8b"
region     = "us-central1"

app      = "app1"
customer = "demo"
env_name = "dev"

image = "ghcr.io/mangesh-deshmane/app1:sha-2a07e91"

# Created by install.sh. Terraform references it but cannot create or alter it.
runtime_service_account = "apoc-app1-runtime@agentic-poc-5b8b.iam.gserviceaccount.com"

min_instance_count = 0
max_instance_count = 2
memory             = "512Mi"
cpu                = "1"

ttl_hours = 24
protected = false
