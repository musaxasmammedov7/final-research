variable "aws_region" {
  description = "AWS Region для развертывания кластера (например, eu-central-1 Франкфурт или us-east-1)"
  type        = string
  default     = "eu-central-1"
}

variable "project_name" {
  description = "Префикс имен ресурсов в AWS"
  type        = string
  default     = "k8s-live-migration"
}

variable "master_instance_type" {
  description = "Тип EC2 инстанса для Control Plane (k8s-master)"
  type        = string
  default     = "t3.medium"
}

variable "worker_instance_type" {
  description = "Тип EC2 инстанса для Worker нод (m6i.large — выделенные ресурсы CPU без троттлинга CPU-кредитов t3 для точных бенчмарков)"
  type        = string
  default     = "m6i.large"
}

variable "allowed_ssh_cidr" {
  description = "CIDR для доступа по SSH (по умолчанию 0.0.0.0/0 по ключу)"
  type        = string
  default     = "0.0.0.0/0"
}

variable "duckdns_domain" {
  description = "Имя поддомена на DuckDNS (без .duckdns.org)"
  type        = string
  default     = "musa-research"
}

variable "duckdns_token" {
  description = "Секретный токен DuckDNS (хранится ТОЛЬКО локально в terraform.tfvars, не попадает в Git!)"
  type        = string
  sensitive   = true
  default     = ""
}

