class SQLQueries:
    SELECT_USER_BY_TG_ID = "SELECT * FROM users WHERE telegram_id = %s"
    INSERT_USER = "\n        INSERT INTO users\n        (id, phone, telegram_id, firstname, lastname, username,\n         profileoptions, options, accountstatus, updatetime, lastseen)\n        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)\n    "
    INSERT_USER_DATA = "\n        INSERT INTO user_data\n        (phone, user_config, chat_config)\n        VALUES (%s, %s, %s)\n    "
    INSERT_DEFAULT_FOLDER = "\n        INSERT INTO user_folders\n        (id, phone, title, sort_order)\n        VALUES ('all.chat.folder', %s, 'Все', 0)\n    "
