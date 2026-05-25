-- Setup inicial do MySQL para o AquaG20.
-- Rode UMA vez como root:
--     mysql -u root -p < scripts\setup_mysql.sql
--
-- Cria o usuário `aquag20`, os databases `aquag20` e `aquag20_test`
-- e concede as permissões necessárias. A senha do usuário está
-- sincronizada com .env (DATABASE_URL).

CREATE USER IF NOT EXISTS 'aquag20'@'localhost'
    IDENTIFIED BY 'MGRKz3SYDjoTz8SSibiGk244ACLIC75D';

CREATE DATABASE IF NOT EXISTS aquag20
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE DATABASE IF NOT EXISTS aquag20_test
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

GRANT ALL PRIVILEGES ON aquag20.*      TO 'aquag20'@'localhost';
GRANT ALL PRIVILEGES ON aquag20_test.* TO 'aquag20'@'localhost';

FLUSH PRIVILEGES;

SELECT User, Host FROM mysql.user WHERE User = 'aquag20';
SHOW DATABASES LIKE 'aquag20%';
