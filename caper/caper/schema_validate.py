#!/usr/bin/env python3

import argparse
import json
import os
import sys
import re
import copy
from jsonschema import Draft7Validator
from typing import Any, Dict, List, Optional, Tuple

# Import the database utilities from utils module
# Adjust the import based on your project structure
try:
    # When imported as a module within the same package
    from .utils import get_db_handle, mongo_client, mongo_client_primary
except ImportError:
    # When run as a standalone script
    from utils import get_db_handle, mongo_client, mongo_client_primary


def generate_schema(data: Any) -> Dict[str, Any]:
    """
    Generates a basic JSON schema from a Python object (dict, list, str, int, etc.).

    Handles nested objects and lists. For lists, the 'items' schema is based
    on the schema of the first element found. If the list is empty, 'items'
    will be an empty schema ({}), allowing any type.

    Args:
        data: The Python object representing JSON data. Can be a dict, list,
              str, int, float, bool, or None.

    Returns:
        A dictionary representing the inferred JSON schema.

    Raises:
        TypeError: If an unsupported data type is encountered.
    """
    schema: Dict[str, Any] = {}  # Initialize the schema dictionary

    if isinstance(data, dict):
        # Handle dictionaries (JSON objects)
        schema["type"] = "object"
        properties: Dict[str, Any] = {}
        required_keys: List[str] = []
        if data:  # Check if the dictionary is not empty
            for key, value in data.items():
                # Recursively generate schema for each value
                properties[key] = generate_schema(value)
                # Assume all keys found are required for this basic version
                required_keys.append(key)
            schema["properties"] = properties
            if required_keys:
                schema["required"] = sorted(required_keys)  # Sort for consistency
        else:
            # Empty object
            schema["properties"] = {}
            # schema["required"] = [] # Optional: explicitly add empty required list

    elif isinstance(data, list):
        # Handle lists (JSON arrays)
        schema["type"] = "array"
        if data:  # Check if the list is not empty
            # --- Basic approach: Infer schema from the first item ---
            first_item_schema = generate_schema(data[0])
            schema["items"] = first_item_schema
        else:
            # Empty list: Define 'items' as an empty schema, allowing any type.
            schema["items"] = {}

    elif isinstance(data, str):
        schema["type"] = "string"
    elif isinstance(data, bool):
        schema["type"] = "boolean"
    elif isinstance(data, int):
        # JSON Schema distinguishes between integer and number
        schema["type"] = "integer"
    elif isinstance(data, float):
        schema["type"] = "number"
    elif data is None:
        schema["type"] = "null"
    else:
        # Handle types that don't directly map to JSON Schema standard types
        raise TypeError(f"Unsupported data type for JSON schema generation: {type(data)}")

    return schema


def parse_arguments():
    """Parses command-line arguments."""
    # Get the database name from environment variable as default
    default_db_name = os.environ.get("DB_NAME", "caper")

    parser = argparse.ArgumentParser(
        description="Validate documents in a MongoDB collection against a JSON schema."
    )
    parser.add_argument(
        "--db", "-d",
        dest="db_host",
        default=None,
        help="MongoDB connection string (if not provided, will use the one from utils)"
    )
    parser.add_argument(
        "--db-name",
        dest="db_name",
        default=default_db_name,
        help=f"Name of the database to use (default: {default_db_name})"
    )
    parser.add_argument(
        "--collection", "-c",
        dest="collection_name",
        default="projects",
        help="Name of the MongoDB collection to validate (default: projects)"
    )
    parser.add_argument(
        "--schema", "-s",
        dest="schema_path",
        default="schema/schema.json",
        help="Path to the JSON schema file (default: schema/schema.json)"
    )
    return parser.parse_args()


def load_schema(schema_path):
    """Loads the JSON schema from the specified file path."""
    try:
        with open(schema_path, 'r', encoding='utf-8') as f:
            schema = json.load(f)
        print(f"Successfully loaded schema from '{schema_path}'")
        return schema
    except FileNotFoundError:
        print(f"Error: Schema file not found at '{schema_path}'", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in schema file '{schema_path}': {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error loading schema file '{schema_path}': {e}", file=sys.stderr)
        sys.exit(1)


def validate_collection(
        schema: Dict[str, Any],
        collection_name: str = "projects",
        db_handle=None,
        client=None,
        close_connection: bool = False
) -> Tuple[int, int, int]:
    """
    Validates documents in a collection against a schema.

    Args:
        schema: The JSON schema to validate against
        collection_name: Name of the MongoDB collection to validate
        db_handle: Optional database handle (if None, uses the one from utils)
        client: Optional MongoDB client (if None, uses the one from utils)
        close_connection: Whether to close the client connection when done

    Returns:
        A tuple of (total documents, invalid documents, error count)
    """
    doc_count = 0
    validated_count = 0
    skipped_deleted_count = 0
    invalid_count = 0
    error_count = 0

    try:
        collection = db_handle[collection_name]
        Draft7Validator.check_schema(schema)
        validator = Draft7Validator(schema)
        print(f"Using database: '{db_handle.name}', collection: '{collection_name}'")

        # Fetch and Validate Documents
        print(f"\n--- Validating documents in '{collection_name}' ---")

        cursor = collection.find()
        for doc in cursor:
            doc_count += 1

            # Skip deleted projects
            deleted = doc.get('delete', False)
            if deleted:
                skipped_deleted_count += 1
                continue

            validated_count += 1

            # --- Identify the document ---
            # Use 'project_name' or 'name' field if available, otherwise use _id
            doc_id_str = str(doc.get('_id', 'UNKNOWN_ID'))
            identifier = doc.get('project_name', doc.get('name', f"Document (ID: {doc_id_str})"))
            creator = doc.get('creator', 'Unknown User')

            # --- Prepare document for validation ---
            # Create a copy and remove MongoDB's _id field
            doc_to_validate = doc.copy()
            if '_id' in doc_to_validate:
                del doc_to_validate['_id']

            # --- Perform Validation ---
            try:
                validation_errors = sorted(
                    validator.iter_errors(doc_to_validate),
                    key=lambda error: (
                        tuple(str(part) for part in error.absolute_path),
                        error.message,
                    ),
                )
                if not validation_errors:
                    print(f"- '{identifier}' ({creator}): VALID")
                    continue

                invalid_count += 1
                print(f"- '{identifier}' ({creator}): NOT VALID")
                for error_number, error in enumerate(validation_errors, start=1):
                    print(f"  Error {error_number}:")
                    print(f"    Reason: {error.message}")
                    if error.absolute_path:
                        path = "/".join(map(str, error.absolute_path))
                        print(f"    Path: /{path}")
                    else:
                        print("    Path: /")
                    if error.validator:
                        print(f"    Schema Keyword: '{error.validator}'")
            except Exception as e:  # Catch unexpected errors during validation for *this* doc
                error_count += 1
                print(f"- '{identifier}': ERROR during validation")
                print(f"  Unexpected validation error: {type(e).__name__} - {e}")

        # Check if any documents were processed
        if doc_count == 0:
            print(f"\nWarning: No documents found in collection '{collection_name}'.")

        # Print Summary
        print("\n--- Validation Summary ---")
        print(f"Total documents processed: {doc_count}")
        print(f"Documents validated: {validated_count}")
        print(f"Deleted documents skipped: {skipped_deleted_count}")
        print(f"Valid documents: {validated_count - invalid_count - error_count}")
        print(f"Invalid documents: {invalid_count}")
        if error_count > 0:
            print(f"Errors during processing: {error_count}")

        return doc_count, invalid_count, error_count

    except Exception as e:
        print(f"Error during validation: {e}", file=sys.stderr)
        return doc_count, invalid_count, error_count + 1


def run_validation(
        db_host: Optional[str] = None,
        db_name: Optional[str] = None,
        collection_name: str = "projects",
        schema_path: str = "schema/schema.json"
) -> str:
    """
    Run the validation process and return the output as a string.
    This function can be called from other Python modules.

    Args:
        db_host: MongoDB connection string (if None, uses the one from utils)
        db_name: Name of the database to use (if None, uses environment variable or default)
        collection_name: Name of the MongoDB collection to validate
        schema_path: Path to the JSON schema file

    Returns:
        A string containing the validation report
    """
    import io
    import sys

    # Capture stdout
    original_stdout = sys.stdout
    captured_output = io.StringIO()
    sys.stdout = captured_output

    try:
        # Use default db_name if not provided
        if db_name is None:
            db_name = os.environ.get("DB_NAME", "caper")

        # Load schema
        schema = load_schema(schema_path)

        # Set up database connection if needed
        client = None
        db_handle = None

        if db_host:
            # If a database host is provided, create a new connection
            db_handle, client = get_db_handle(db_name, db_host)
            print(f"Connected to MongoDB at '{db_host}', database '{db_name}'")
        else:
            # Use the existing connection from utils, but with the specified db_name
            db_handle = mongo_client[db_name]
            client = mongo_client
            print(f"Using existing MongoDB connection from utils, database '{db_name}'")

        # Run validation
        validate_collection(
            schema=schema,
            collection_name=collection_name,
            db_handle=db_handle,
            client=client,
            close_connection=bool(db_host)  # Only close if we created a new connection
        )

        # Return the captured output
        return captured_output.getvalue()

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return captured_output.getvalue()

    finally:
        # Restore stdout
        sys.stdout = original_stdout


def get_default_value(prop_schema):
    """
    Determines the default value for a field based on its JSON schema definition.
    Uses an explicit schema ``default`` first. Otherwise it prefers ``null`` if
    allowed, then falls back to type-specific empty values.
    """
    if not isinstance(prop_schema, dict):
        return None  # Fallback for malformed or non-existent schema part

    if "default" in prop_schema:
        return copy.deepcopy(prop_schema["default"])

    schema_type = prop_schema.get("type")

    type_options = []
    if isinstance(schema_type, list):
        type_options.extend(schema_type)
    elif schema_type is not None:
        type_options.append(schema_type)

    if "null" in type_options:
        return None

    primary_type = None
    if schema_type:
        if isinstance(schema_type, list):
            # Pick the first non-null type, or the first type if all are non-null, or None if list is empty
            primary_type = next((t for t in schema_type if t != "null"), schema_type[0] if schema_type else None)
        else:  # schema_type is a string
            primary_type = schema_type

    # primary_type is None if "type" was not specified (e.g. "tarfile": {}) or type list was empty/only contained "null"
    # (already handled)
    if primary_type == "string":
        return ""
    elif primary_type == "number":
        return 0.0
    elif primary_type == "integer":
        return 0
    elif primary_type == "boolean":
        return False
    elif primary_type == "array":
        return []
    elif primary_type == "object":
        return {}

    return None  # Default for unspecified types (e.g., "tarfile": {}) or unhandled types


def _format_change_path(path_parts):
    """Format dictionary/list path parts for the human-readable repair report."""
    formatted = ""
    for part in path_parts:
        if isinstance(part, int):
            formatted += f"[{part}]"
        else:
            if formatted:
                formatted += "."
            formatted += str(part)
    return formatted


def _mongo_change_path(path_parts):
    """Convert dictionary/list path parts to MongoDB dotted update syntax."""
    return ".".join(str(part) for part in path_parts)


def _record_change(changes_log, path_parts, action, value):
    changes_log.append({
        "path": _format_change_path(path_parts),
        "path_parts": tuple(path_parts),
        "action": action,
        "value_set": copy.deepcopy(value),
    })


def _add_missing_fields_recursive(document_part, schema_node, changes_log, current_path_parts):
    """
    Recursively adds missing required fields to the document_part based on schema_node.
    Modifies document_part in place. Logs changes to changes_log.
    Returns True if changes were made, False otherwise.
    """
    made_change_at_this_level_or_below = False

    if not isinstance(document_part, dict) or not isinstance(schema_node, dict):
        return False

    # 1. Add missing required fields defined in the current schema_node
    for req_key in schema_node.get("required", []):
        if req_key not in document_part:
            prop_schema_for_req_key = schema_node.get("properties", {}).get(req_key)
            if prop_schema_for_req_key is None:  # Key is required but no definition in properties
                prop_schema_for_req_key = {}  # Treat as an empty schema, will yield a 'null' default

            default_val = get_default_value(prop_schema_for_req_key)
            document_part[req_key] = default_val

            # Special case for FINISHED? - if the key is "FINISHED?", set it to True by default
            if req_key == "FINISHED?" and isinstance(default_val, bool):
                document_part[req_key] = True

            _record_change(
                changes_log,
                current_path_parts + [req_key],
                "added_missing_required_key",
                document_part[req_key],
            )
            made_change_at_this_level_or_below = True

            # If the added field is an object, recurse to fill its required fields
            if isinstance(default_val, dict) and isinstance(prop_schema_for_req_key, dict) and \
                    (prop_schema_for_req_key.get("properties") or
                     prop_schema_for_req_key.get("patternProperties") or
                     prop_schema_for_req_key.get("required")):
                # current_path_parts + [req_key] forms the path to the newly added object
                if _add_missing_fields_recursive(document_part[req_key], prop_schema_for_req_key, changes_log,
                                                 current_path_parts + [req_key]):
                    # This recursive call will set made_change_at_this_level_or_below if it makes further changes
                    pass  # The flag is already true from adding req_key

    # 2. Traverse existing fields for nested structures (objects, arrays of objects, patternProperties)
    for key, value in list(document_part.items()):  # list() for safe iteration if mods occur (unlikely in this loop)

        field_schema_definition = schema_node.get("properties", {}).get(key)

        if field_schema_definition is None and "patternProperties" in schema_node:
            for pattern, pattern_schema_node_def in schema_node.get("patternProperties", {}).items():
                if isinstance(pattern, str) and isinstance(key, str) and re.match(pattern, key):
                    field_schema_definition = pattern_schema_node_def
                    break

        if not isinstance(field_schema_definition, dict):
            continue  # No schema definition for this key, or definition is not a dict

        current_field_path_parts = current_path_parts + [key]

        if isinstance(value, dict):  # Includes objects matched by properties or patternProperties
            if _add_missing_fields_recursive(value, field_schema_definition, changes_log, current_field_path_parts):
                made_change_at_this_level_or_below = True

        elif isinstance(value, list) and field_schema_definition.get("type") == "array":
            item_schema_definition = field_schema_definition.get("items")
            if isinstance(item_schema_definition, dict):  # If items are objects defined by a schema
                for i, item in enumerate(value):
                    if isinstance(item, dict):
                        # Path for an item in an array: e.g., parent.arrayKey[index]
                        item_path_parts = current_path_parts + [key, i]
                        if _add_missing_fields_recursive(item, item_schema_definition, changes_log, item_path_parts):
                            made_change_at_this_level_or_below = True

    return made_change_at_this_level_or_below


def _normalize_legacy_visibility(document, schema, changes_log, issues_log):
    """
    Convert the two legacy boolean visibility values to their canonical strings.

    Unknown values are deliberately left untouched: silently coercing them to
    private would hide corrupt data from the QC report.
    """
    visibility_schema = schema.get("properties", {}).get("private", {})
    allowed_values = visibility_schema.get("enum", [])
    if "private" not in document:
        return False

    current_value = document["private"]
    if type(current_value) is bool:
        canonical_value = "private" if current_value else "public"
        if canonical_value in allowed_values:
            document["private"] = canonical_value
            _record_change(
                changes_log,
                ["private"],
                "normalized_legacy_visibility",
                canonical_value,
            )
            return True

    if allowed_values and current_value not in allowed_values:
        issues_log.append({
            "path": "private",
            "reason": (
                f"Unsupported visibility value {current_value!r}; "
                "left unchanged for manual review"
            ),
        })

    return False


def _get_path_value(document, path_parts):
    value = document
    for part in path_parts:
        value = value[part]
    return value


def _build_set_updates(document, changes_log):
    """
    Build a non-overlapping MongoDB $set document from the recorded changes.

    Selecting the shortest changed paths first avoids conflicting updates such
    as setting both ``runs`` and ``runs.sample_1.0.Feature_ID``.
    """
    unique_paths = sorted(
        {tuple(change["path_parts"]) for change in changes_log},
        key=lambda path: (len(path), tuple(str(part) for part in path)),
    )
    selected_paths = []
    for path in unique_paths:
        if any(path[:len(parent)] == parent for parent in selected_paths):
            continue
        selected_paths.append(path)

    return {
        _mongo_change_path(path): copy.deepcopy(_get_path_value(document, path))
        for path in selected_paths
    }


def _validation_error_summary(error):
    path = "/" + "/".join(map(str, error.absolute_path))
    return {
        "path": path,
        "reason": error.message,
        "validator": error.validator,
    }


def _format_report_field(value, fallback="Unknown"):
    """Return a concise, readable value for document metadata in reports."""
    if value is None or value == "":
        return fallback
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def run_fix_schema(
        db_host: Optional[str] = None,
        db_name: Optional[str] = None,
        collection_name: str = "projects",
        schema_path: str = "schema/schema.json",
        apply_changes: bool = False,
) -> str:
    schema = load_schema(schema_path)
    Draft7Validator.check_schema(schema)
    validator = Draft7Validator(schema)
    if db_name is None:
        db_name = os.environ.get("DB_NAME", "caper")

    if db_host:
        # If a database host is provided, create a new connection
        db_handle, client = get_db_handle(db_name, db_host)
        print(f"Connected to MongoDB at '{db_host}', database '{db_name}'")
    else:
        # Repairs must read from and write to the primary so a preview/apply
        # cycle never plans changes from a stale secondary snapshot.
        db_handle = mongo_client_primary[db_name]
        client = mongo_client_primary
        print(f"Using primary MongoDB connection from utils, database '{db_name}'")
    db = client[db_name]
    collection = db[collection_name]

    overall_report = {}
    documents_processed = 0
    documents_skipped = 0
    documents_with_repairs = 0
    documents_updated = 0
    documents_with_unresolved_errors = 0

    for doc in collection.find():
        documents_processed += 1
        doc_id_str = str(doc["_id"])

        # Skip deleted projects
        deleted = doc.get('delete', False)
        if deleted:
            documents_skipped += 1
            continue

        current_doc_changes_log = []
        current_doc_issues_log = []
        made_changes_to_this_doc = _normalize_legacy_visibility(
            doc,
            schema,
            current_doc_changes_log,
            current_doc_issues_log,
        )
        if _add_missing_fields_recursive(doc, schema, current_doc_changes_log, []):
            made_changes_to_this_doc = True

        remaining_errors = sorted(
            validator.iter_errors({
                key: value for key, value in doc.items()
                if key != "_id"
            }),
            key=lambda error: (
                tuple(str(part) for part in error.absolute_path),
                error.message,
            ),
        )
        if remaining_errors or current_doc_issues_log:
            documents_with_unresolved_errors += 1

        if made_changes_to_this_doc:
            documents_with_repairs += 1
            if apply_changes:
                set_updates = _build_set_updates(doc, current_doc_changes_log)
                if set_updates:
                    collection.update_one(
                        {"_id": doc["_id"]},
                        {"$set": set_updates},
                    )
                    documents_updated += 1

        if made_changes_to_this_doc or current_doc_issues_log or remaining_errors:
            overall_report[doc_id_str] = {
                "project_name": _format_report_field(
                    doc.get("project_name", doc.get("name")),
                    fallback="Unknown project",
                ),
                "creator": _format_report_field(doc.get("creator")),
                "date_created": _format_report_field(doc.get("date_created")),
                "changes": current_doc_changes_log,
                "issues": current_doc_issues_log,
                "remaining_errors": [
                    _validation_error_summary(error)
                    for error in remaining_errors
                ],
            }

    mode = "APPLY" if apply_changes else "DRY RUN"
    fix_schema_report = f"\n--- Schema Repair Report ({mode}) ---"
    if not apply_changes:
        fix_schema_report += "\nPreview only: no MongoDB documents were modified."
    if not overall_report:
        fix_schema_report += "\nNo repairable schema changes or visibility issues were found."
    else:
        for doc_id, document_report in overall_report.items():
            fix_schema_report += (
                f"\nProject: '{document_report['project_name']}'"
                f"\n  Document ID: {doc_id}"
                f"\n  Creator: {document_report['creator']}"
                f"\n  Created: {document_report['date_created']}"
            )
            for change in document_report["changes"]:
                path = change['path']
                action = change['action']
                value = change['value_set']

                value_str = json.dumps(value)  # Use json.dumps for faithful representation
                if len(value_str) > 70:
                    value_str = value_str[:67] + "..."
                fix_schema_report += (
                    f"\n  - Path: '{path}', Action: {action}, "
                    f"Value Set: {value_str}"
                )
            for issue in document_report["issues"]:
                fix_schema_report += (
                    f"\n  - Path: '{issue['path']}', "
                    f"Manual review required: {issue['reason']}"
                )
            if document_report["remaining_errors"]:
                fix_schema_report += "\n  Remaining validation errors after proposed repairs:"
                for error in document_report["remaining_errors"]:
                    fix_schema_report += (
                        f"\n    - Path: {error['path']}, "
                        f"Reason: {error['reason']}"
                    )

    fix_schema_report += (
        f"\nSummary:"
        f"\n  Documents processed: {documents_processed}"
        f"\n  Deleted documents skipped: {documents_skipped}"
        f"\n  Documents with repairable changes: {documents_with_repairs}"
        f"\n  Updated documents: {documents_updated}"
        f"\n  Documents requiring further review: {documents_with_unresolved_errors}"
    )
    return fix_schema_report


def main():
    """Main function for command-line usage."""
    args = parse_arguments()

    try:
        # Load schema
        schema = load_schema(args.schema_path)

        # Set up database connection if needed

        if args.db_host:
            # If a database host is provided, create a new connection
            # Use the db_name from arguments
            db_handle, client = get_db_handle(args.db_name, args.db_host)
            print(f"Connected to MongoDB at '{args.db_host}', database '{args.db_name}'")
        else:
            # Use the existing connection from utils, but with the specified db_name
            db_handle = mongo_client[args.db_name]
            client = mongo_client
            print(f"Using existing MongoDB connection from utils, database '{args.db_name}'")

        # Run validation
        validate_collection(
            schema=schema,
            collection_name=args.collection_name,
            db_handle=db_handle,
            client=client,
            close_connection=bool(args.db_host)  # Only close if we created a new connection
        )

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
