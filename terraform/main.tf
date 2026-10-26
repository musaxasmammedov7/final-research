terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# 1. Автоматический поиск свежего официального образа Ubuntu 24.04 LTS (amd64, Kernel 6.8+)
data "aws_ami" "ubuntu_24_04" {
  most_recent = true
  owners      = ["099720109477"] # Canonical official owner ID

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

# 2. Автоматическая генерация SSH-ключа (сохраняется локально в k8s-research-key.pem)
resource "tls_private_key" "ssh_key" {
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "aws_key_pair" "research_key" {
  key_name   = "${var.project_name}-key"
  public_key = tls_private_key.ssh_key.public_key_openssh
}

resource "local_file" "private_key_pem" {
  content         = tls_private_key.ssh_key.private_key_pem
  filename        = "${path.module}/k8s-research-key.pem"
  file_permission = "0600"
}

# 3. Сеть (VPC + 1 общая подсеть в одной Availability Zone для задержки <0.3ms)
resource "aws_vpc" "research_vpc" {
  cidr_block           = "10.10.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name = "${var.project_name}-vpc"
  }
}

resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.research_vpc.id

  tags = {
    Name = "${var.project_name}-igw"
  }
}

resource "aws_subnet" "public_subnet" {
  vpc_id                  = aws_vpc.research_vpc.id
  cidr_block              = "10.10.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true

  tags = {
    Name = "${var.project_name}-subnet"
  }
}

resource "aws_route_table" "public_rt" {
  vpc_id = aws_vpc.research_vpc.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }

  tags = {
    Name = "${var.project_name}-rt"
  }
}

resource "aws_route_table_association" "public_rta" {
  subnet_id      = aws_subnet.public_subnet.id
  route_table_id = aws_route_table.public_rt.id
}

# 4. Security Group (Полный доступ внутри кластера для K8s + Cilium eBPF + CRIU Stream, и SSH снаружи)
resource "aws_security_group" "k8s_sg" {
  name        = "${var.project_name}-sg"
  description = "Security group for K8s CRIU Live Migration & Cilium Service Mesh cluster"
  vpc_id      = aws_vpc.research_vpc.id

  # Весь трафик между самими нодами кластера (K8s API, Cilium VXLAN/Geneve, Hubble, CRIU memory transfer)
  ingress {
    description = "Allow all internal cluster traffic between nodes"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    self        = true
  }

  # SSH доступ
  ingress {
    description = "SSH access"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.allowed_ssh_cidr]
  }

  # Kubernetes API Server (6443) и NodePort диапазон для демо-тестов (30000-32767)
  ingress {
    description = "Kubernetes API and NodePorts for Demo"
    from_port   = 6443
    to_port     = 6443
    protocol    = "tcp"
    cidr_blocks = [var.allowed_ssh_cidr]
  }

  ingress {
    description = "NodePort Services for Live Migration Demo"
    from_port   = 30000
    to_port     = 32767
    protocol    = "tcp"
    cidr_blocks = [var.allowed_ssh_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-sg"
  }
}

# 5. Инстанс 1: Control Plane (k8s-master)
resource "aws_instance" "k8s_master" {
  ami                    = data.aws_ami.ubuntu_24_04.id
  instance_type          = var.master_instance_type
  subnet_id              = aws_subnet.public_subnet.id
  vpc_security_group_ids = [aws_security_group.k8s_sg.id]
  key_name               = aws_key_pair.research_key.key_name
  private_ip             = "10.10.1.10"

  # ВАЖНО: Отключаем Source/Destination Check для бесшовной маршрутизации Cilium и миграции IP подов
  source_dest_check = false

  root_block_device {
    volume_size           = 30
    volume_type           = "gp3"
    delete_on_termination = true
  }

  tags = {
    Name = "k8s-master"
    Role = "master"
  }
}

# 6. Инстансы 2 и 3: Worker Nodes (k8s-worker-1, k8s-worker-2)
resource "aws_instance" "k8s_workers" {
  count                  = 2
  ami                    = data.aws_ami.ubuntu_24_04.id
  instance_type          = var.worker_instance_type
  subnet_id              = aws_subnet.public_subnet.id
  vpc_security_group_ids = [aws_security_group.k8s_sg.id]
  key_name               = aws_key_pair.research_key.key_name
  private_ip             = "10.10.1.${21 + count.index}"

  # ВАЖНО: Отключаем Source/Destination Check для живой миграции Pod IP между воркерами
  source_dest_check = false

  root_block_device {
    volume_size           = 40
    volume_type           = "gp3"
    delete_on_termination = true
  }

  tags = {
    Name = "k8s-worker-${count.index + 1}"
    Role = "worker"
  }
}

# 7. Автоматическая генерация inventory.ini для Ansible и env-файла
resource "local_file" "ansible_inventory" {
  content = <<-EOT
    [master]
    k8s-master ansible_host=${aws_instance.k8s_master.public_ip} private_ip=${aws_instance.k8s_master.private_ip}

    [workers]
    k8s-worker-1 ansible_host=${aws_instance.k8s_workers[0].public_ip} private_ip=${aws_instance.k8s_workers[0].private_ip}
    k8s-worker-2 ansible_host=${aws_instance.k8s_workers[1].public_ip} private_ip=${aws_instance.k8s_workers[1].private_ip}

    [k8s_cluster:children]
    master
    workers

    [k8s_cluster:vars]
    ansible_user=ubuntu
    ansible_ssh_private_key_file=${abspath("${path.module}/k8s-research-key.pem")}
    ansible_ssh_common_args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'
    ansible_python_interpreter=/usr/bin/python3
    master_private_ip=${aws_instance.k8s_master.private_ip}
  EOT
  filename        = "${path.module}/../ansible/inventory.ini"
  file_permission = "0644"
}

resource "local_file" "cluster_env" {
  content = <<-EOT
    export SSH_KEY="${abspath("${path.module}/k8s-research-key.pem")}"
    export MASTER_PUBLIC_IP="${aws_instance.k8s_master.public_ip}"
    export MASTER_PRIVATE_IP="${aws_instance.k8s_master.private_ip}"
    export WORKER1_PUBLIC_IP="${aws_instance.k8s_workers[0].public_ip}"
    export WORKER1_PRIVATE_IP="${aws_instance.k8s_workers[0].private_ip}"
    export WORKER2_PUBLIC_IP="${aws_instance.k8s_workers[1].public_ip}"
    export WORKER2_PRIVATE_IP="${aws_instance.k8s_workers[1].private_ip}"
  EOT
  filename        = "${path.module}/cluster_ips.env"
  file_permission = "0644"
}

