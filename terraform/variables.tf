variable "aws_region" {
  description = "AWS Region for cluster deployment (e.g., eu-central-1 Frankfurt or us-east-1)"
  type        = string
  default     = "eu-central-1"
}

variable "project_name" {
  description = "Resource name prefix in AWS"
  type        = string
  default     = "k8s-live-migration"
}

variable "master_instance_type" {
  description = "EC2 instance type for the Control Plane (k8s-master, c7i-flex.large — 2 vCPU, 4 GB RAM, Free Tier eligible)"
  type        = string
  default     = "c7i-flex.large"
}

variable "worker_instance_type" {
  description = "EC2 instance type for Worker nodes (m7i-flex.large — 2 vCPU, 8 GB RAM, Intel Xeon 4th Gen, Free Tier eligible)"
  type        = string
  default     = "m7i-flex.large"
}

variable "allowed_ssh_cidr" {
  description = "Allowed CIDR block for SSH and Ingress access (default 0.0.0.0/0)"
  type        = string
  default     = "0.0.0.0/0"
}

variable "duckdns_domain" {
  description = "DuckDNS subdomain name (without .duckdns.org)"
  type        = string
  default     = "musa-research"
}

variable "duckdns_token" {
  description = "Secret DuckDNS token (stored locally in git-ignored terraform.tfvars only)"
  type        = string
  sensitive   = true
  default     = ""
}
