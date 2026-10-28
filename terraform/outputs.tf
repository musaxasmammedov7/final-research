output "master_public_ip" {
  description = "Public IP address of k8s-master"
  value       = aws_instance.k8s_master.public_ip
}

output "worker_1_public_ip" {
  description = "Public IP address of k8s-worker-1 (Source Node)"
  value       = aws_instance.k8s_workers[0].public_ip
}

output "worker_2_public_ip" {
  description = "Public IP address of k8s-worker-2 (Target Node)"
  value       = aws_instance.k8s_workers[1].public_ip
}

output "ssh_command_master" {
  description = "Ready-to-use SSH command to connect to k8s-master"
  value       = "ssh -i ${path.module}/k8s-research-key.pem ubuntu@${aws_instance.k8s_master.public_ip}"
}
