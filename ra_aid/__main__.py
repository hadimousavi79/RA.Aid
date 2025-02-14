import argparse
import os
import sys
import uuid
from datetime import datetime

from langgraph.checkpoint.memory import MemorySaver
from rich.console import Console
from rich.panel import Panel

from ra_aid import print_error, print_stage_header
from ra_aid.__version__ import __version__
from ra_aid.agent_utils import (
    AgentInterrupt,
    create_agent,
    run_agent_with_retry,
    run_planning_agent,
    run_research_agent,
)
from ra_aid.config import (
    DEFAULT_MAX_TEST_CMD_RETRIES,
    DEFAULT_RECURSION_LIMIT,
    VALID_PROVIDERS,
)
from ra_aid.dependencies import check_dependencies
from ra_aid.env import validate_environment
from ra_aid.llm import initialize_llm
from ra_aid.logging_config import get_logger, setup_logging
from ra_aid.project_info import format_project_info, get_project_info
from ra_aid.prompts import CHAT_PROMPT, WEB_RESEARCH_PROMPT_SECTION_CHAT
from ra_aid.tool_configs import get_chat_tools
from ra_aid.tools.human import ask_human
from ra_aid.tools.memory import _global_memory

logger = get_logger(__name__)


def launch_webui(host: str, port: int):
    """Launch the RA.Aid web interface."""
    from ra_aid.webui import run_server

    print(f"Starting RA.Aid web interface on http://{host}:{port}")
    run_server(host=host, port=port)


def parse_arguments(args=None):
    ANTHROPIC_DEFAULT_MODEL = "claude-3-5-sonnet-20241022"
    OPENAI_DEFAULT_MODEL = "gpt-4o"

    parser = argparse.ArgumentParser(
        description="RA.Aid - AI Agent for executing programming and research tasks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    ra-aid -m "Add error handling to the database module"
    ra-aid -m "Explain the authentication flow" --research-only
        """,
    )
    parser.add_argument(
        "-m",
        "--message",
        type=str,
        help="The task or query to be executed by the agent",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show program version number and exit",
    )
    parser.add_argument(
        "--research-only",
        action="store_true",
        help="Only perform research without implementation",
    )
    parser.add_argument(
        "--provider",
        type=str,
        default=(
            "openai"
            if (os.getenv("OPENAI_API_KEY") and not os.getenv("ANTHROPIC_API_KEY"))
            else "anthropic"
        ),
        choices=VALID_PROVIDERS,
        help="The LLM provider to use",
    )
    parser.add_argument("--model", type=str, help="The model name to use")
    parser.add_argument(
        "--research-provider",
        type=str,
        choices=VALID_PROVIDERS,
        help="Provider to use specifically for research tasks",
    )
    parser.add_argument(
        "--research-model",
        type=str,
        help="Model to use specifically for research tasks",
    )
    parser.add_argument(
        "--planner-provider",
        type=str,
        choices=VALID_PROVIDERS,
        help="Provider to use specifically for planning tasks",
    )
    parser.add_argument(
        "--planner-model", type=str, help="Model to use specifically for planning tasks"
    )
    parser.add_argument(
        "--cowboy-mode",
        action="store_true",
        help="Skip interactive approval for shell commands",
    )
    parser.add_argument(
        "--expert-provider",
        type=str,
        default="openai",
        choices=VALID_PROVIDERS,
        help="The LLM provider to use for expert knowledge queries (default: openai)",
    )
    parser.add_argument(
        "--expert-model",
        type=str,
        help="The model name to use for expert knowledge queries (required for non-OpenAI providers)",
    )
    parser.add_argument(
        "--hil",
        "-H",
        action="store_true",
        help="Enable human-in-the-loop mode, where the agent can prompt the user for additional information.",
    )
    parser.add_argument(
        "--chat",
        action="store_true",
        help="Enable chat mode with direct human interaction (implies --hil)",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Enable verbose logging output"
    )
    parser.add_argument(
        "--pretty-logger", action="store_true", help="Enable pretty logging output"
    )
    parser.add_argument(
        "--temperature",
        type=float,
        help="LLM temperature (0.0-2.0). Controls randomness in responses",
        default=None,
    )
    parser.add_argument(
        "--disable-limit-tokens",
        action="store_false",
        help="Whether to disable token limiting for Anthropic Claude react agents. Token limiter removes older messages to prevent maximum token limit API errors.",
    )
    parser.add_argument(
        "--no-fallback-tool",
        action="store_true",
        help="Disable fallback model switching.",
    )
    parser.add_argument(
        "--experimental-fallback-handler",
        action="store_true",
        help="Enable experimental fallback handler.",
    )
    parser.add_argument(
        "--fallback-tool-models",
        type=str,
        default="gpt-3.5-turbo,gpt-4",
        help="Comma-separated list of fallback models to use in order.",
    )
    parser.add_argument(
        "--recursion-limit",
        type=int,
        default=DEFAULT_RECURSION_LIMIT,
        help="Maximum recursion depth for agent operations (default: 100)",
    )
    parser.add_argument(
        "--aider-config", type=str, help="Specify the aider config file path"
    )
    parser.add_argument(
        "--test-cmd",
        type=str,
        help="Test command to run before completing tasks (e.g. 'pytest tests/')",
    )
    parser.add_argument(
        "--auto-test",
        action="store_true",
        help="Automatically run tests before completing tasks",
    )
    parser.add_argument(
        "--max-test-cmd-retries",
        type=int,
        default=DEFAULT_MAX_TEST_CMD_RETRIES,
        help="Maximum number of retries for the test command (default: 10)",
    )
    parser.add_argument(
        "--webui",
        action="store_true",
        help="Launch the web interface",
    )
    parser.add_argument(
        "--webui-host",
        type=str,
        default="0.0.0.0",
        help="Host to listen on for web interface (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--webui-port",
        type=int,
        default=8080,
        help="Port to listen on for web interface (default: 8080)",
    )
    if args is None:
        args = sys.argv[1:]
    parsed_args = parser.parse_args(args)

    # Set hil=True when chat mode is enabled
    if parsed_args.chat:
        parsed_args.hil = True

    # Validate provider
    if parsed_args.provider not in VALID_PROVIDERS:
        parser.error(f"Invalid provider: {parsed_args.provider}")
    # Handle model defaults and requirements

    if parsed_args.provider == "openai":
        parsed_args.model = parsed_args.model or OPENAI_DEFAULT_MODEL
    elif parsed_args.provider == "anthropic":
        # Always use default model for Anthropic
        parsed_args.model = ANTHROPIC_DEFAULT_MODEL
    elif not parsed_args.model and not parsed_args.research_only:
        # Require model for other providers unless in research mode
        parser.error(
            f"--model is required when using provider '{parsed_args.provider}'"
        )

    # Handle expert provider/model defaults
    if not parsed_args.expert_provider:
        # If no expert provider specified, use main provider instead of defaulting to
        # to any particular model since we do not know if we have access to any other model.
        parsed_args.expert_provider = parsed_args.provider
        parsed_args.expert_model = parsed_args.model

    # Validate temperature range if provided
    if parsed_args.temperature is not None and not (
        0.0 <= parsed_args.temperature <= 2.0
    ):
        parser.error("Temperature must be between 0.0 and 2.0")

    # Validate recursion limit is positive
    if parsed_args.recursion_limit <= 0:
        parser.error("Recursion limit must be positive")

    # if auto-test command is provided, validate test-cmd is also provided
    if parsed_args.auto_test and not parsed_args.test_cmd:
        parser.error("Test command is required when using --auto-test")

    return parsed_args


# Create console instance
console = Console()

# Create individual memory objects for each agent
research_memory = MemorySaver()
planning_memory = MemorySaver()
implementation_memory = MemorySaver()


def is_informational_query() -> bool:
    """Determine if the current query is informational based on implementation_requested state."""
    return _global_memory.get("config", {}).get(
        "research_only", False
    ) or not is_stage_requested("implementation")


def is_stage_requested(stage: str) -> bool:
    """Check if a stage has been requested to proceed."""
    if stage == "implementation":
        return _global_memory.get("implementation_requested", False)
    return False


def main():
    """Main entry point for the ra-aid command line tool."""
    args = parse_arguments()
    setup_logging(args.verbose, args.pretty_logger)
    logger.debug("Starting RA.Aid with arguments: %s", args)

    # Launch web interface if requested
    if args.webui:
        launch_webui(args.webui_host, args.webui_port)
        return

    try:
        # Check dependencies before proceeding
        check_dependencies()

        expert_enabled, expert_missing, web_research_enabled, web_research_missing = (
            validate_environment(args)
        )  # Will exit if main env vars missing
        logger.debug("Environment validation successful")

        if expert_missing:
            console.print(
                Panel(
                    "[yellow]Expert tools disabled due to missing configuration:[/yellow]\n"
                    + "\n".join(f"- {m}" for m in expert_missing)
                    + "\nSet the required environment variables or args to enable expert mode.",
                    title="Expert Tools Disabled",
                    style="yellow",
                )
            )

        if web_research_missing:
            console.print(
                Panel(
                    "[yellow]Web research disabled due to missing configuration:[/yellow]\n"
                    + "\n".join(f"- {m}" for m in web_research_missing)
                    + "\nSet the required environment variables to enable web research.",
                    title="Web Research Disabled",
                    style="yellow",
                )
            )

        # Handle chat mode
        if args.chat:
            # Initialize chat model with default provider/model
            chat_model = initialize_llm(
                args.provider, args.model, temperature=args.temperature
            )
            if args.research_only:
                print_error("Chat mode cannot be used with --research-only")
                sys.exit(1)

            print_stage_header("Chat Mode")

            # Get project info
            try:
                project_info = get_project_info(".", file_limit=2000)
                formatted_project_info = format_project_info(project_info)
            except Exception as e:
                logger.warning(f"Failed to get project info: {e}")
                formatted_project_info = ""

            # Get initial request from user
            initial_request = ask_human.invoke(
                {"question": "What would you like help with?"}
            )

            # Get working directory and current date
            working_directory = os.getcwd()
            current_date = datetime.now().strftime("%Y-%m-%d")

            # Run chat agent with CHAT_PROMPT
            config = {
                "configurable": {"thread_id": str(uuid.uuid4())},
                "recursion_limit": args.recursion_limit,
                "chat_mode": True,
                "cowboy_mode": args.cowboy_mode,
                "hil": True,  # Always true in chat mode
                "web_research_enabled": web_research_enabled,
                "initial_request": initial_request,
                "limit_tokens": args.disable_limit_tokens,
            }

            # Store config in global memory
            _global_memory["config"] = config
            _global_memory["config"]["provider"] = args.provider
            _global_memory["config"]["model"] = args.model
            _global_memory["config"]["expert_provider"] = args.expert_provider
            _global_memory["config"]["expert_model"] = args.expert_model

            # Create chat agent with appropriate tools
            chat_agent = create_agent(
                chat_model,
                get_chat_tools(
                    expert_enabled=expert_enabled,
                    web_research_enabled=web_research_enabled,
                ),
                checkpointer=MemorySaver(),
            )

            # Run chat agent and exit
            run_agent_with_retry(
                chat_agent,
                CHAT_PROMPT.format(
                    initial_request=initial_request,
                    web_research_section=(
                        WEB_RESEARCH_PROMPT_SECTION_CHAT if web_research_enabled else ""
                    ),
                    working_directory=working_directory,
                    current_date=current_date,
                    project_info=formatted_project_info,
                ),
                config,
            )
            return

        # Validate message is provided
        if not args.message:
            print_error("--message is required")
            sys.exit(1)

        base_task = args.message
        config = {
            "configurable": {"thread_id": str(uuid.uuid4())},
            "recursion_limit": args.recursion_limit,
            "research_only": args.research_only,
            "cowboy_mode": args.cowboy_mode,
            "web_research_enabled": web_research_enabled,
            "aider_config": args.aider_config,
            "limit_tokens": args.disable_limit_tokens,
            "auto_test": args.auto_test,
            "test_cmd": args.test_cmd,
            "max_test_cmd_retries": args.max_test_cmd_retries,
            "experimental_fallback_handler": args.experimental_fallback_handler,
        }

        # Store config in global memory for access by is_informational_query
        _global_memory["config"] = config

        # Store base provider/model configuration
        _global_memory["config"]["provider"] = args.provider
        _global_memory["config"]["model"] = args.model

        # Store expert provider/model (no fallback)
        _global_memory["config"]["expert_provider"] = args.expert_provider
        _global_memory["config"]["expert_model"] = args.expert_model

        # Store planner config with fallback to base values
        _global_memory["config"]["planner_provider"] = (
            args.planner_provider or args.provider
        )
        _global_memory["config"]["planner_model"] = args.planner_model or args.model

        _global_memory["config"]["no_fallback_tool"] = args.no_fallback_tool

        # Store research config with fallback to base values
        _global_memory["config"]["research_provider"] = (
            args.research_provider or args.provider
        )
        _global_memory["config"]["research_model"] = args.research_model or args.model

        # Store fallback tool configuration
        _global_memory["config"]["no_fallback_tool"] = args.no_fallback_tool

        # Run research stage
        print_stage_header("Research Stage")

        # Initialize research model with potential overrides
        research_provider = args.research_provider or args.provider
        research_model_name = args.research_model or args.model
        research_model = initialize_llm(
            research_provider, research_model_name, temperature=args.temperature
        )

        run_research_agent(
            base_task,
            research_model,
            expert_enabled=expert_enabled,
            research_only=args.research_only,
            hil=args.hil,
            memory=research_memory,
            config=config,
        )

        # Proceed with planning and implementation if not an informational query
        if not is_informational_query():
            # Initialize planning model with potential overrides
            planner_provider = args.planner_provider or args.provider
            planner_model_name = args.planner_model or args.model
            planning_model = initialize_llm(
                planner_provider, planner_model_name, temperature=args.temperature
            )

            # Run planning agent
            run_planning_agent(
                base_task,
                planning_model,
                expert_enabled=expert_enabled,
                hil=args.hil,
                memory=planning_memory,
                config=config,
            )

    except (KeyboardInterrupt, AgentInterrupt):
        print()
        print(" 👋 Bye!")
        print()
        sys.exit(0)


if __name__ == "__main__":
    main()
