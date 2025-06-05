#!/bin/bash
#docker run -d --name neo4j -p 7474:7474 -p 7687:7687 --env NEO4J_AUTH=neo4j/$NEO4J_PASSWORD_SECRET -v /home/ubuntu/AmpliconRepository-dev/neo4j neo4j
. ./caper/config.sh

# docker run -d --name neo4j -p 7474:7474 -p 7687:7687 --env NEO4J_AUTH=neo4j/$NEO4J_PASSWORD_SECRET ---volume=/home/ubuntu/AmpliconRepository-${AMPLICON_ENV}/neo4j/conf:/conf neo4j:2025.03.0

docker run -d --name neo4j -p 7474:7474 -p 7687:7687 --env NEO4J_AUTH=neo4j/$NEO4J_PASSWORD_SECRET -v /home/ubuntu/AmpliconRepository-${AMPLICON_ENV}/neo4j:/neo4j neo4j:2025.03.0
