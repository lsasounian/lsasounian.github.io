#!/bin/bash
set -e

awslocal sqs create-queue \
  --queue-name conversation-history.fifo \
  --attributes '{"FifoQueue":"true","ContentBasedDeduplication":"false"}'

echo "[localstack-init] fila conversation-history.fifo criada"
