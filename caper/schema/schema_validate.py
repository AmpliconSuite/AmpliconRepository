#!/usr/bin/env python3

import argparse
import json
import os
import sys
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, OperationFailure, ConfigurationError
from jsonschema import validate, ValidationError
from typing import Any, Dict, List


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
    schema: Dict[str, Any] = {} # Initialize the schema dictionary

    if isinstance(data, dict):
        # Handle dictionaries (JSON objects)
        schema["type"] = "object"
        properties: Dict[str, Any] = {}
        required_keys: List[str] = []
        if data: # Check if the dictionary is not empty
            for key, value in data.items():
                # Recursively generate schema for each value
                properties[key] = generate_schema(value)
                # Assume all keys found are required for this basic version
                required_keys.append(key)
            schema["properties"] = properties
            if required_keys:
                schema["required"] = sorted(required_keys) # Sort for consistency
        else:
            # Empty object
            schema["properties"] = {}
            # schema["required"] = [] # Optional: explicitly add empty required list

    elif isinstance(data, list):
        # Handle lists (JSON arrays)
        schema["type"] = "array"
        if data: # Check if the list is not empty
            # --- Basic approach: Infer schema from the first item ---
            # A more complex approach could analyze all items to find a common
            # schema or use 'anyOf'/'oneOf' for mixed types.
            first_item_schema = generate_schema(data[0])
            schema["items"] = first_item_schema

            # --- (Optional) More robust approach idea: ---
            # item_schemas = [generate_schema(item) for item in data]
            # unique_schemas = {json.dumps(s, sort_keys=True) for s in item_schemas}
            # if len(unique_schemas) == 1:
            #     schema["items"] = item_schemas[0] # All items have the same schema
            # elif len(unique_schemas) > 1:
                 # Mixed types - Use 'anyOf' or a simple empty schema {}
            #     # schema["items"] = {"anyOf": [json.loads(s) for s in unique_schemas]} # More complex
            #     schema["items"] = {} # Simple fallback: allow any type
            # else: # Should not happen if list is not empty
            #     schema["items"] = {}
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


# We don't strictly need ObjectId, but it's good practice if manipulating _id
# from bson import ObjectId

def parse_arguments():
    """Parses command-line arguments."""
    # Get the database name from environment variable or raise an error if not set
    db_name = os.environ.get("DB_NAME")
    if db_name is None:
        print("Error: Environment variable DB_NAME is not set.", file=sys.stderr)
        sys.exit(1)

    default_db_uri = f"mongodb://localhost:27017/{db_name}"

    parser = argparse.ArgumentParser(
        description="Validate documents in a MongoDB collection against a JSON schema."
    )
    parser.add_argument(
        "--db", "-d",
        dest="db_host",
        default=default_db_uri,
        help=f"MongoDB connection string (default: {default_db_uri})"
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
        default="schema.json",
        help="Path to the JSON schema file (default: schema.json)"
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


def main():
    """Main function to connect, fetch, and validate documents."""
    args = parse_arguments()
    schema = load_schema(args.schema_path)

    client = None # Initialize client to None for finally block
    try:
        # 1. Connect to MongoDB
        print(f"Connecting to MongoDB at '{args.db_host}'...")
        # Add timeout to prevent hanging indefinitely
        client = MongoClient(args.db_host, serverSelectionTimeoutMS=5000)

        # The ismaster command is cheap and does not require auth.
        client.admin.command('ismaster')
        print("MongoDB connection successful.")

        # Determine the database to use
        # Try getting default from URI, otherwise print warning/error or use a default
        db_name_from_uri = client.get_database().name # Gets default from URI or 'test'
        if db_name_from_uri == 'test' and '/test' not in args.db_host:
             # Check if 'test' is really the intended DB if not specified in URI
             print(f"Warning: No database specified in connection string. Using default database: '{db_name_from_uri}'.", file=sys.stderr)
             # Or exit: print("Error: No database specified in connection string.", file=sys.stderr); sys.exit(1)

        db = client.get_database() # Use the default database from URI or MongoClient default
        collection = db[args.collection_name]
        print(f"Using database: '{db.name}', collection: '{args.collection_name}'")


    except (ConnectionFailure, ConfigurationError) as e:
        print(f"Error: Could not connect to MongoDB at '{args.db_host}': {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e: # Catch other potential mongo errors during connection/setup
         print(f"Error connecting to MongoDB or accessing collection: {e}", file=sys.stderr)
         sys.exit(1)


    # 2. Fetch and Validate Documents
    print(f"\n--- Validating documents in '{args.collection_name}' ---")
    doc_count = 0
    invalid_count = 0
    error_count = 0

    try:
        cursor = collection.find()
        for doc in cursor:
            doc_count += 1

            # Skip deleted projects
            deleted = doc.get('delete', False)
            if deleted: continue

            # --- Identify the document ---
            # Use 'project_name' or 'name' field if available, otherwise use _id
            doc_id_str = str(doc.get('_id', 'UNKNOWN_ID'))
            identifier = doc.get('project_name', doc.get('name', f"Document (ID: {doc_id_str})"))
            creator = doc.get('creator', 'Unknown User')

            # --- Prepare document for validation ---
            # Create a copy and remove MongoDB's _id field, as it's usually
            # not part of the user-defined schema. If your schema *does*
            # include _id, you might skip this step.
            doc_to_validate = doc.copy()
            if '_id' in doc_to_validate:
                del doc_to_validate['_id']

            # --- Perform Validation ---
            try:
                validate(instance=doc_to_validate, schema=schema)
                print(f"- '{identifier}' ({creator}): VALID")
            except ValidationError as e:
                invalid_count += 1
                print(f"- '{identifier}' ({creator}): NOT VALID")
                # Provide specific error details
                print(f"  Reason: {e.message}") # Primary error message
                if e.path:
                     # e.path is a deque, convert to list for cleaner printing
                    print(f"  Path: /{' / '.join(map(str, e.path))}")
                if e.validator:
                    print(f"  Schema Keyword: '{e.validator}'")
                # Can add more details from e.context, e.schema_path etc. if needed
            except Exception as e: # Catch unexpected errors during validation for *this* doc
                error_count += 1
                print(f"- '{identifier}': ERROR during validation")
                print(f"  Unexpected validation error: {type(e).__name__} - {e}")

        # Check if any documents were processed
        if doc_count == 0:
             print(f"\nWarning: No documents found in collection '{args.collection_name}'.")

    except OperationFailure as e:
         print(f"\nError during MongoDB operation (e.g., permission issue reading collection): {e}", file=sys.stderr)
         error_count += 1 # Count this as an error preventing further processing
    except Exception as e: # Catch errors during the find() loop itself
        print(f"\nAn unexpected error occurred while fetching documents: {e}", file=sys.stderr)
        error_count += 1 # Count this as an error
    finally:
        # 3. Print Summary and Close Connection
        print("\n--- Validation Summary ---")
        print(f"Total documents processed: {doc_count}")
        print(f"Valid documents: {doc_count - invalid_count - error_count}")
        print(f"Invalid documents: {invalid_count}")
        if error_count > 0:
            print(f"Errors during processing: {error_count}")

        if client:
            client.close()
            print("MongoDB connection closed.")


if __name__ == "__main__":
    main()