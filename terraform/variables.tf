variable "project_id" {
  type        = string
  description = "GCP project to deploy into."

  validation {
    condition     = can(regex("^agentic-poc-", var.project_id))
    error_message = "Refusing to deploy outside an agentic-poc-* project."
  }
}

variable "region" {
  type        = string
  description = "Cloud Run region."
  default     = "us-central1"

  validation {
    condition     = contains(["us-central1", "asia-south1"], var.region)
    error_message = "Region must be us-central1 or asia-south1 (allowlisted for free tier)."
  }
}

variable "customer" {
  type        = string
  description = "Customer slug this environment is for. Becomes part of the service name."
  default     = "demo"

  validation {
    # RFC1035: Cloud Run rejects anything else, and an LLM-supplied string is
    # exactly where a bad name would come from.
    condition     = can(regex("^[a-z]([-a-z0-9]{0,18}[a-z0-9])?$", var.customer))
    error_message = "customer must be lowercase alphanumeric with hyphens, 1-20 chars, starting with a letter."
  }
}

variable "app" {
  type        = string
  description = "Which application blueprint to deploy."
  default     = "app1"

  validation {
    condition     = contains(["app1", "app2"], var.app)
    error_message = "app must be app1 or app2."
  }
}

variable "env_name" {
  type        = string
  description = "Environment name. Also selects the state prefix."
  default     = "dev"

  validation {
    condition     = contains(["dev", "test"], var.env_name)
    error_message = "env_name must be dev or test. No production from this pipeline."
  }
}

variable "image" {
  type        = string
  description = "Container image to run. Use a digest in CI; a tag is fine by hand."
  default     = "gcr.io/cloudrun/hello"
}

# ---- cost guardrails -------------------------------------------------------
# These defaults are the free-tier contract. The policy gate in Phase 2 asserts
# the same values against the plan JSON, so a change here alone will not pass.

variable "min_instance_count" {
  type        = number
  description = "Minimum instances. MUST stay 0 - anything above bills continuously."
  default     = 0

  validation {
    condition     = var.min_instance_count == 0
    error_message = "min_instance_count must be 0. A warm instance costs roughly $13/month."
  }
}

variable "max_instance_count" {
  type        = number
  description = "Maximum instances."
  default     = 2

  validation {
    condition     = var.max_instance_count >= 1 && var.max_instance_count <= 3
    error_message = "max_instance_count must be between 1 and 3."
  }
}

variable "memory" {
  type        = string
  description = "Memory per instance."
  default     = "512Mi"

  validation {
    condition     = contains(["256Mi", "512Mi"], var.memory)
    error_message = "memory must be 256Mi or 512Mi."
  }
}

variable "cpu" {
  type        = string
  description = "vCPU per instance."
  default     = "1"

  validation {
    condition     = contains(["1"], var.cpu)
    error_message = "cpu must be 1."
  }
}

variable "ttl_hours" {
  type        = number
  description = "How long this environment should live. Read by the teardown job."
  default     = 24

  validation {
    condition     = var.ttl_hours >= 1 && var.ttl_hours <= 168
    error_message = "ttl_hours must be between 1 and 168 (one week)."
  }
}

variable "protected" {
  type        = bool
  description = "When true, the scheduled teardown skips this environment."
  default     = false
}
