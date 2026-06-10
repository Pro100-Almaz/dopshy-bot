ALTER TABLE academy_groups ADD CONSTRAINT academy_groups_group_name_group_type_key
UNIQUE (group_name, group_type);