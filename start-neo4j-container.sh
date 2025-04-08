#!/bin/bash
#docker run -d --name neo4j -p 7474:7474 -p 7687:7687 --env NEO4J_AUTH=neo4j/$NEO4J_PASSWORD_SECRET -v /home/ubuntu/AmpliconRepository-dev/neo4j neo4j

docker run -d --name neo4j -p 7474:7474 -p 7687:7687 --env NEO4J_AUTH=neo4j/$NEO4J_PASSWORD_SECRET -v /home/ubuntu/AmpliconRepository-dev/neo4j neo4j

