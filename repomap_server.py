import asyncio
import json
import os
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any, Set
from contextlib import asynccontextmanager

from fastmcp import FastMCP, Settings
from repomap_class import RepoMap
from utils import count_tokens, read_text
from scm import get_scm_fname
from importance import filter_important_files

# Helper function from your CLI, useful to have here
def find_src_files(directory: str) -> List[str]:
    if not os.path.isdir(directory):
        return [directory] if os.path.isfile(directory) else []
    src_files = []
    for r, d, f_list in os.walk(directory):
        d[:] = [d_name for d_name in d if not d_name.startswith('.') and d_name not in {'node_modules', '__pycache__', 'venv', 'env'}]
        for f in f_list:
            if not f.startswith('.'):
                src_files.append(os.path.join(r, f))
    return src_files

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(levelname)-5s %(asctime)-15s %(name)s:%(funcName)s:%(lineno)d - %(message)s')
log = logging.getLogger(__name__)

class ProjectConfig:
    def __init__(self, config_path: str):
        self.config_path = Path(config_path)
        log.info(f"ProjectConfig initialized with config_path: {self.config_path.absolute()}")
        self.config = self._load_config()
    
    def _load_config(self) -> dict:
        log.info(f"Attempting to load config from: {self.config_path.absolute()}")
        if not self.config_path.exists():
            log.warning(f"Config file not found at {self.config_path.absolute()}. Creating an empty one.")
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, 'w') as f:
                json.dump({"projects": {}}, f) # Initialize with empty projects
        try:
            with open(self.config_path) as f:
                config_data = json.load(f)
                log.info(f"Successfully loaded config. Projects found: {len(config_data.get('projects', {}))}")
                return config_data
        except json.JSONDecodeError as e:
            log.error(f"Error decoding JSON from {self.config_path}: {e}")
            # Optionally, handle corrupted file: backup and create new empty config
            # For now, re-raise to indicate a problem
            raise
        except Exception as e:
            log.error(f"An unexpected error occurred while loading config: {e}")
            raise
    
    def get_project_root(self, project_name: str) -> str:
        projects = self.config.get('projects', {})
        for key, project_data in projects.items():
            if key.lower() == project_name.lower():
                return project_data.get('root', '')
        return ''

# --- Global RepoMap Instance and Server Setup ---
# This will be initialized during server startup
project_config: Optional[ProjectConfig] = None

@asynccontextmanager
async def lifespan(mcp_server: FastMCP):
    global project_config
    
    log.info("RepoMap MCP Server starting up...")
    log.info(f"Current working directory for server: {os.getcwd()}")
    
    # Initialize ProjectConfig
    script_dir = Path(__file__).resolve().parent
    config_file_path = script_dir / "projects.json"
    project_config = ProjectConfig(str(config_file_path))
    log.info(f"ProjectConfig initialized in lifespan. Config path: {project_config.config_path.absolute()}")
    log.info(f"Projects loaded in lifespan: {project_config.config.get('projects', {}).keys()}")
    
    yield
    
    log.info("RepoMap MCP Server shutting down...")

mcp = FastMCP("RepoMapServer", stateless_http=True, lifespan=lifespan)

@mcp.tool()
async def repo_map(
    project_name: str,
    chat_files: Optional[List[str]] = None,
    other_files: Optional[List[str]] = None,
    token_limit: int = 8192,
    exclude_unranked: bool = False,
    force_refresh: bool = False,
    mentioned_files: Optional[List[str]] = None,
    mentioned_idents: Optional[List[str]] = None,
    verbose: bool = False,
    max_context_window: Optional[int] = None,
) -> Dict[str, Any]:
    """Generate a repository map for the specified files, providing a list of function prototypes and variables for files as well as relevant related
    files. When providing filenames, it's crucial that all files are given as absolute paths. If relative paths are provided, they will 
    be resolved against the project root. In addition to the files provided, relevant related files will also be included with a
    very small ranking boost.

    :param project_name: The name of the project to map. This name is used to look up the project's root directory in 'projects.json'.
    :param chat_files: A list of file paths that are currently in the chat context. These files will receive the highest ranking.
    :param other_files: A list of other relevant file paths in the repository to consider for the map. They receive a lower ranking boost than mentioned_files and chat_files.
    :param token_limit: The maximum number of tokens the generated repository map should occupy. Defaults to 8192.
    :param exclude_unranked: If True, files with a PageRank of 0.0 will be excluded from the map. Defaults to False.
    :param force_refresh: If True, forces a refresh of the repository map cache. Defaults to False.
    :param mentioned_files: Optional list of file paths explicitly mentioned in the conversation and receive a mid-level ranking boost.
    :param mentioned_idents: Optional list of identifiers explicitly mentioned in the conversation, to boost their ranking.
    :param verbose: If True, enables verbose logging for the RepoMap generation process. Defaults to False.
    :param max_context_window: Optional maximum context window size for token calculation, used to adjust map token limit when no chat files are provided.
    :returns: A dictionary containing the generated repository map under the 'map' key, or an 'error' key if an error occurred.
    """
    global project_config

    if project_config is None:
        log.error("Server not fully initialized. ProjectConfig is missing.")
        return {"error": "Server not fully initialized. ProjectConfig is missing."}

    project_root = project_config.get_project_root(project_name)
    
    if not project_root:
        log.warning(f"Project '{project_name}' not found in projects.json")
        return {"error": f"Project '{project_name}' not found in projects.json"}
    
    # 1. Handle optional arguments
    chat_files_list = chat_files or []
    mentioned_fnames_set = set(mentioned_files) if mentioned_files else None
    mentioned_idents_set = set(mentioned_idents) if mentioned_idents else None

    # 2. If a specific list of other_files isn't provided, scan the whole root directory.
    # This should happen regardless of whether chat_files are present.
    effective_other_files = []
    if other_files:
        effective_other_files = other_files
    else:
        log.info("No other_files provided, scanning root directory for context...")
        effective_other_files = find_src_files(project_root)

    # Add a print statement for debugging so you can see what the tool is working with.
    log.debug(f"Chat files: {chat_files_list}")
    log.debug(f"Effective other_files count: {len(effective_other_files)}")

    # If after all that we have no files, we can exit early.
    if not chat_files_list and not effective_other_files:
        log.info("No files to process.")
        return {"map": "No files found to generate a map."}

    # 3. Resolve paths to be absolute
    root_path = Path(project_root).resolve()
    abs_chat_files = [str(Path(f).resolve()) for f in chat_files_list]
    abs_other_files = [str(Path(f).resolve()) for f in effective_other_files]
    
    # Remove any chat files from the other_files list to avoid duplication
    abs_chat_files_set = set(abs_chat_files)
    abs_other_files = [f for f in abs_other_files if f not in abs_chat_files_set]

    # 4. Instantiate and run RepoMap
    try:
        repo_mapper = RepoMap(
            map_tokens=token_limit,
            root=str(root_path),
            token_counter_func=lambda text: count_tokens(text, "gpt-4"),
            file_reader_func=read_text,
            output_handler_funcs={'info': log.info, 'warning': log.warning, 'error': log.error},
            verbose=verbose,
            exclude_unranked=exclude_unranked,
            max_context_window=max_context_window
        )
    except Exception as e:
        log.exception(f"Failed to initialize RepoMap for project '{project_name}': {e}")
        return {"error": f"Failed to initialize RepoMap: {str(e)}"}

    try:
        map_content = await asyncio.to_thread(
            repo_mapper.get_repo_map,
            chat_files=abs_chat_files,
            other_files=abs_other_files,
            mentioned_fnames=mentioned_fnames_set,
            mentioned_idents=mentioned_idents_set,
            force_refresh=force_refresh
        )
        return {"map": map_content or "No repository map could be generated."}
    except Exception as e:
        log.exception(f"Error generating repository map for project '{project_name}': {e}")
        return {"error": f"Error generating repository map: {str(e)}"}

@mcp.tool()
async def list_projects() -> Dict[str, Any]:
    """Returns a list of configured projects, including their key, root, and description.

    :returns: A dictionary containing a list of projects under the 'projects' key, or an 'error' key if an error occurred.
    """
    global project_config

    if project_config is None:
        log.error("Server not fully initialized. ProjectConfig is missing.")
        return {"error": "Server not fully initialized. ProjectConfig is missing."}

    log.info(f"Attempting to list projects. ProjectConfig.config: {project_config.config}")
    try:
        projects_data = []
        for project_key, project_details in project_config.config.get("projects", {}).items():
            projects_data.append({
                "key": project_key,
                "root": project_details.get("root", ""),
                "description": project_details.get("description", "No description provided.")
            })
        log.info(f"Successfully prepared projects data: {projects_data}")
        return {"projects": projects_data}
    except Exception as e:
        log.exception(f"Error listing projects: {e}")
        return {"error": f"Error listing projects: {str(e)}"}
    
@mcp.tool()
async def search_identifiers(
    project_name: str,
    query: str,
    max_results: int = 50,
    context_lines: int = 2,
    include_definitions: bool = True,
    include_references: bool = True
) -> Dict[str, Any]:
    """Search for identifiers in code files. Get back a list of matching identifiers with their file, line number, and context.
       When searching, just use the identifier name without any special characters, prefixes or suffixes. The search is 
       case-insensitive.

    Args:
        project_name: Name of the project to search in
        query: Search query (identifier name)
        max_results: Maximum number of results to return
        context_lines: Number of lines of context to show
        include_definitions: Whether to include definition occurrences
        include_references: Whether to include reference occurrences
    
    Returns:
        Dictionary containing search results or error message
    """
    global project_config

    if project_config is None:
        log.error("Server not fully initialized. ProjectConfig is missing.")
        return {"error": "Server not fully initialized. ProjectConfig is missing."}

    project_root = project_config.get_project_root(project_name)
    if not project_root:
        log.warning(f"Project '{project_name}' not found in projects.json")
        return {"error": f"Project '{project_name}' not found in projects.json"}

    try:
        # Initialize RepoMap with search-specific settings
        repo_map = RepoMap(
            root=project_root,
            token_counter_func=lambda text: count_tokens(text, "gpt-4"),
            file_reader_func=read_text,
            output_handler_funcs={'info': log.info, 'warning': log.warning, 'error': log.error},
            verbose=False,
            exclude_unranked=True
        )

        # Find all source files in the project
        all_files = find_src_files(project_root)
        
        # Get all tags (definitions and references) for all files
        all_tags = []
        for file_path in all_files:
            rel_path = str(Path(file_path).relative_to(project_root))
            tags = repo_map.get_tags(file_path, rel_path)
            all_tags.extend(tags)

        # Filter tags based on search query and options
        matching_tags = []
        query_lower = query.lower()
        
        for tag in all_tags:
            if query_lower in tag.name.lower():
                if (tag.kind == "def" and include_definitions) or \
                   (tag.kind == "ref" and include_references):
                    matching_tags.append(tag)

        # Sort by relevance (definitions first, then references)
        matching_tags.sort(key=lambda x: (x.kind != "def", x.name.lower().find(query_lower)))

        # Limit results
        matching_tags = matching_tags[:max_results]

        # Format results with context
        results = []
        for tag in matching_tags:
            file_path = str(Path(project_root) / tag.rel_fname)
            
            # Calculate context range based on context_lines parameter
            start_line = max(1, tag.line - context_lines)
            end_line = tag.line + context_lines
            context_range = list(range(start_line, end_line + 1))
            
            context = repo_map.render_tree(
                file_path,
                tag.rel_fname,
                context_range
            )
            
            if context:
                results.append({
                    "file": tag.rel_fname,
                    "line": tag.line,
                    "name": tag.name,
                    "kind": tag.kind,
                    "context": context
                })

        return {"results": results}

    except Exception as e:
        log.exception(f"Error searching identifiers in project '{project_name}': {e}")
        return {"error": f"Error searching identifiers: {str(e)}"}    

# --- Main Entry Point ---
def main():
    # Run the MCP server
    log.info("Starting FastMCP server...")
    mcp.run()

if __name__ == "__main__":
    main()
