# GitHub Copilot Instructions for AmpliconRepository

## Critical Django Management Command Requirements

**IMPORTANT**: Before running ANY `python manage.py <command>` or Django-related commands in this project, you **MUST** first source the environment configuration file:

```bash
source caper/config.sh
```

### Why This Is Required

The `caper/config.sh` file sets essential environment variables including:
- Database connection strings (MongoDB)
- Secret keys for OAuth (Google, Globus)
- AWS/S3 configuration
- Email settings
- Site URLs and domain settings
- Neo4j credentials
- Django admin credentials

Without sourcing this file, Django commands will fail or use incorrect configuration.

### Correct Command Pattern

❌ **WRONG:**
```bash
cd caper
python manage.py migrate
```

✅ **CORRECT:**
```bash
source caper/config.sh
cd caper
python manage.py migrate
```

Or as a single command:
```bash
source caper/config.sh && cd caper && python manage.py migrate
```

### Helper Script Available

A helper script is provided that automatically sources the config file:

```bash
./run_django_command.sh migrate
./run_django_command.sh createsuperuser
./run_django_command.sh runserver
```

### When Writing Scripts

If you're creating new shell scripts that run Django commands, always include:

```bash
#!/bin/bash
source caper/config.sh
# ... rest of your script
```

### Environment-Specific Config Files

- `caper/config.sh` - Main configuration (development/production)
- Ensure the correct config file is sourced for the target environment

### Terminal Commands in AI Coding Assistants

When suggesting terminal commands to the user, always prefix Django management commands with:

```bash
source caper/config.sh && cd caper && python manage.py <command>
```

This is a **hard requirement** for this project and should never be omitted.
