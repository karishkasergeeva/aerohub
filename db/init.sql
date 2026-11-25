-- Окремі бази даних для кожного сервісу (database-per-service)
CREATE DATABASE flight_db;
CREATE DATABASE resource_db;
CREATE DATABASE fids_db;
\connect resource_db
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE DATABASE acdm_db;
CREATE DATABASE task_db;
