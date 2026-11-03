CREATE TABLE account_security (user_id BIGINT PRIMARY KEY,password_hash TEXT NOT NULL,hint TEXT NOT NULL,updated BIGINT NOT NULL);
CREATE TABLE `auth_tokens` (
    `phone` VARCHAR(20) NOT NULL,
    `token_hash` VARCHAR(64) NOT NULL,
    `code_hash` VARCHAR(64) NOT NULL,
    `expires` VARCHAR(16) NOT NULL,
    `state` VARCHAR(16), attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (`phone`, `token_hash`)
);
CREATE TABLE `banners` (
    `id` VARCHAR(64) NOT NULL,
    `title` VARCHAR(256) NOT NULL,
    `description` VARCHAR(512) NOT NULL,
    `url` VARCHAR(512) NOT NULL,
    `type` INT NOT NULL DEFAULT 1,
    `priority` INT NOT NULL DEFAULT 0,
    `animoji_id` INT NOT NULL DEFAULT 0,
    `repeat` INT NOT NULL DEFAULT 1,
    `rerun` BIGINT NOT NULL DEFAULT 0,
    `hide_close_button` BOOLEAN NOT NULL DEFAULT FALSE,
    `hide_on_click` BOOLEAN NOT NULL DEFAULT FALSE,
    `is_title_animated` BOOLEAN NOT NULL DEFAULT FALSE,
    `enabled` BOOLEAN NOT NULL DEFAULT TRUE,
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`)
);
CREATE TABLE bot_registry(bot_id INTEGER PRIMARY KEY,owner_id INTEGER NOT NULL,token_hash TEXT NOT NULL UNIQUE,active INTEGER NOT NULL DEFAULT 1,created INTEGER NOT NULL, commands TEXT NOT NULL DEFAULT '[]');
CREATE TABLE bot_states(bot_id INTEGER NOT NULL,user_id INTEGER NOT NULL,suspended INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(bot_id,user_id));
CREATE TABLE bot_wizard(user_id INTEGER PRIMARY KEY,step TEXT NOT NULL,draft TEXT NOT NULL);
CREATE TABLE chat_admins(chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,permissions INTEGER NOT NULL,PRIMARY KEY(chat_id,user_id));
CREATE TABLE chat_invite_links(id TEXT PRIMARY KEY,chat_id INTEGER NOT NULL,token TEXT NOT NULL UNIQUE,title TEXT NOT NULL DEFAULT '',created INTEGER NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,join_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE `chat_participants` (
    `chat_id` INT NOT NULL,
    `user_id` INT NOT NULL,
    `joined_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`chat_id`, `user_id`)
);
CREATE TABLE chat_read_marks(chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,mark INTEGER NOT NULL DEFAULT 0,marked_unread INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(chat_id,user_id));
CREATE TABLE chat_visibility(chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,hidden INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(chat_id,user_id));
CREATE TABLE `chats` (
    `id` INT NOT NULL,
    `owner` INT NOT NULL,
    `type` VARCHAR(16) NOT NULL, title TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '', created BIGINT NOT NULL DEFAULT 0, options TEXT NOT NULL DEFAULT '{}', avatar_id INTEGER, updated INTEGER NOT NULL DEFAULT 0, pinned_message_id INTEGER, is_deleted INTEGER NOT NULL DEFAULT 0, access TEXT NOT NULL DEFAULT 'PRIVATE', public_slug TEXT,
    PRIMARY KEY (`id`)
);
CREATE TABLE `contacts` (
    `owner_id` INT NOT NULL,
    `contact_id` INT NOT NULL,
    `custom_firstname` VARCHAR(64),
    `custom_lastname` VARCHAR(64),
    `is_blocked` BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (`owner_id`, `contact_id`)
);
CREATE TABLE message_deletions (message_id BIGINT NOT NULL, user_id BIGINT NOT NULL, deleted_at BIGINT NOT NULL, PRIMARY KEY(message_id,user_id));
CREATE TABLE message_reactions (chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,user_id INTEGER NOT NULL,reaction_type TEXT NOT NULL,reaction_id TEXT NOT NULL,updated INTEGER NOT NULL,PRIMARY KEY(message_id,user_id));
CREATE TABLE `messages` (
    `id` BIGINT NOT NULL,
    `chat_id` INT NOT NULL,
    `sender` INT NOT NULL,
    `time` VARCHAR(32) NOT NULL,
    `text` VARCHAR(4000) NOT NULL,
    `attaches` JSON NOT NULL,
    `cid` VARCHAR(32) NOT NULL,
    `elements` JSON NOT NULL,
    `type` VARCHAR(16) NOT NULL,
    PRIMARY KEY (`id`)
);
CREATE TABLE `tokens` (
    `phone` VARCHAR(20) NOT NULL,
    `token_hash` VARCHAR(64) NOT NULL,
    `device_type` VARCHAR(256) NOT NULL,
    `device_name` VARCHAR(256) NOT NULL,
    `location` VARCHAR(256) NOT NULL,
    `time` VARCHAR(16) NOT NULL,
    `push_token` VARCHAR(512) DEFAULT NULL, ip_address TEXT, client_info TEXT,
    PRIMARY KEY (`phone`, `token_hash`)
);
CREATE TABLE `user_data` (
    `phone` VARCHAR(20) NOT NULL UNIQUE,
    `user_config` JSON NOT NULL,
    `chat_config` JSON NOT NULL,
    PRIMARY KEY (`phone`)
);
CREATE TABLE `user_folders` (
    `id` VARCHAR(64) NOT NULL,
    `phone` VARCHAR(20) NOT NULL,
    `title` VARCHAR(128) NOT NULL,
    `filters` JSON NOT NULL DEFAULT ('[]'),
    `include` JSON NOT NULL DEFAULT ('[]'),
    `options` JSON NOT NULL DEFAULT ('[]'),
    `source_id` INT NOT NULL DEFAULT 1,
    `update_time` BIGINT NOT NULL DEFAULT 0,
    `sort_order` INT NOT NULL DEFAULT 0, exclude_ids TEXT NOT NULL DEFAULT '[]', emoji TEXT, favorites TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (`id`, `phone`)
);
CREATE TABLE `users` (
    `id` INT NOT NULL,
    `phone` VARCHAR(20) UNIQUE,
    `telegram_id` VARCHAR(64),
    `firstname` VARCHAR(59) NOT NULL,
    `lastname` VARCHAR(59),
    `description` VARCHAR(400),
    `avatar_id` VARCHAR(16),
    `updatetime` VARCHAR(24),
    `lastseen` VARCHAR(24),
    `profileoptions` JSON NOT NULL,
    `options` JSON NOT NULL,
    `accountstatus` VARCHAR(16) NOT NULL,
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    `username` VARCHAR(60) UNIQUE,
    PRIMARY KEY (`id`)
);
CREATE UNIQUE INDEX chat_public_slug ON chats(public_slug) WHERE public_slug IS NOT NULL;
CREATE INDEX reactions_chat ON message_reactions(chat_id,message_id);
