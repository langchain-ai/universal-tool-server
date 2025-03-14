"""Test the server."""

from contextlib import asynccontextmanager
from typing import Annotated, AsyncGenerator, Optional, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, HTTPStatusError
from starlette.authentication import BaseUser
from starlette.requests import Request
from universal_tool_client import AsyncClient, get_async_client

from universal_tool_server import Server
from universal_tool_server._version import __version__
from universal_tool_server.auth import Auth
from universal_tool_server.tools import InjectedRequest

from ..unit_tests.utils import AnyStr


@asynccontextmanager
async def get_async_test_client(
    server: FastAPI,
    *,
    path: Optional[str] = None,
    raise_app_exceptions: bool = True,
    headers: dict | None = None,
) -> AsyncGenerator[AsyncClient, None]:
    """Get an async client."""
    url = "http://localhost:9999"
    if path:
        url += path
    transport = ASGITransport(
        app=server,
        raise_app_exceptions=raise_app_exceptions,
    )

    client = get_async_client(transport=transport, headers=headers)

    try:
        yield cast(AsyncClient, client)
    finally:
        del client


async def test_health() -> None:
    app = Server()
    async with get_async_test_client(app) as client:
        assert await client.health() == {"status": "OK"}


async def test_info() -> None:
    app = Server()
    async with get_async_test_client(app) as client:
        assert await client.info() == {
            "version": __version__,
        }


async def test_add_langchain_tool() -> None:
    """Test adding a tool that's defined using langchain tool decorator."""
    app = Server()

    # Test prior to adding any tools
    async with get_async_test_client(app) as client:
        tools = await client.tools.list()
        assert tools == []

    @app.add_tool
    async def say_hello() -> str:
        """Say hello."""
        return "Hello"

    @app.add_tool
    async def echo(msg: str) -> str:
        """Echo the message back."""
        return msg

    @app.add_tool
    async def add(x: int, y: int) -> int:
        """Add two integers."""
        return x + y

    async with get_async_test_client(app) as client:
        data = await client.tools.list()
        assert data == [
            {
                "description": "Say hello.",
                "id": "say_hello@1.0.0",
                "input_schema": {"properties": {}, "type": "object"},
                "name": "say_hello",
                "output_schema": {"type": "string"},
                "version": "1.0.0",
            },
            {
                "description": "Echo the message back.",
                "id": "echo@1.0.0",
                "input_schema": {
                    "properties": {"msg": {"type": "string"}},
                    "required": ["msg"],
                    "type": "object",
                },
                "name": "echo",
                "output_schema": {"type": "string"},
                "version": "1.0.0",
            },
            {
                "description": "Add two integers.",
                "id": "add@1.0.0",
                "input_schema": {
                    "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                    "required": ["x", "y"],
                    "type": "object",
                },
                "name": "add",
                "output_schema": {"type": "integer"},
                "version": "1.0.0",
            },
        ]


async def test_call_tool() -> None:
    """Test call parameterless tool."""
    app = Server()

    @app.add_tool
    async def say_hello() -> str:
        """Say hello."""
        return "Hello"

    @app.add_tool
    async def add(x: int, y: int) -> int:
        """Add two integers."""
        return x + y

    async with get_async_test_client(app) as client:
        response = await client.tools.call(
            "say_hello",
            {},
        )

        assert response == {
            "call_id": AnyStr(),
            "value": "Hello",
            "success": True,
        }


async def test_create_langchain_tools_from_server() -> None:
    """Test create langchain tools from server."""
    app = Server()

    @app.add_tool
    async def say_hello() -> str:
        """Say hello."""
        return "Hello"

    @app.add_tool
    async def add(x: int, y: int) -> int:
        """Add two integers."""
        return x + y

    async with get_async_test_client(app) as client:
        tools = await client.tools.as_langchain_tools(tool_ids=["say_hello", "add"])
        say_hello_client_side = tools[0]
        add_client_side = tools[1]

        assert await say_hello_client_side.ainvoke({}) == "Hello"
        assert say_hello_client_side.args_schema == {"properties": {}, "type": "object"}

        assert await add_client_side.ainvoke({"x": 1, "y": 2}) == 3
        assert add_client_side.args == {
            "x": {"type": "integer"},
            "y": {"type": "integer"},
        }


class User(BaseUser):
    """User class."""

    @property
    def is_authenticated(self) -> bool:
        """Check if user is authenticated."""
        return True

    @property
    def display_name(self) -> str:
        """Get user's display name."""
        return "Test User"

    @property
    def identity(self) -> str:
        """Get user's identity."""
        return "test-user"


async def test_auth_list_tools() -> None:
    """Test ability to list tools."""

    app = Server()
    auth = Auth()
    app.add_auth(auth)

    @app.add_tool(permissions=["group1"])
    async def say_hello() -> str:
        """Say hello."""
        return "Hello"

    @app.add_tool(permissions=["group2"])
    async def add(x: int, y: int) -> int:
        """Add two integers."""
        return x + y

    @auth.authenticate
    async def authenticate(headers: dict[bytes, bytes]) -> dict:
        """Authenticate incoming requests."""
        # Validate credentials (e.g., API key, JWT token)
        api_key = headers.get(b"x-api-key")
        if not api_key or api_key != b"123":
            raise auth.exceptions.HTTPException(detail="Not authorized")

        return {"permissions": ["group1"], "identity": "some-user"}

    async with get_async_test_client(app, headers={"x-api-key": "123"}) as client:
        tools = await client.tools.list()
        assert tools == [
            {
                "description": "Say hello.",
                "id": "say_hello@1.0.0",
                "input_schema": {"properties": {}, "type": "object"},
                "name": "say_hello",
                "output_schema": {"type": "string"},
                "version": "1.0.0",
            }
        ]

        await client.tools.call("say_hello", {})


async def test_call_tool_with_auth() -> None:
    """Test calling a tool with authentication provided."""
    app = Server()

    @app.add_tool(permissions=["group1"])
    async def say_hello(request: Annotated[Request, InjectedRequest]) -> str:
        """Say hello."""
        return "Hello"

    auth = Auth()

    @auth.authenticate
    async def authenticate(headers: dict[bytes, bytes]) -> dict:
        """Authenticate incoming requests."""
        api_key = headers.get(b"x-api-key")

        api_key_to_user = {
            b"1": {"permissions": ["group1"], "identity": "some-user"},
            b"2": {"permissions": ["group2"], "identity": "another-user"},
        }

        if not api_key or api_key not in api_key_to_user:
            raise auth.exceptions.HTTPException(detail="Not authorized")

        return api_key_to_user[api_key]

    app.add_auth(auth)

    async with get_async_test_client(app, headers={"x-api-key": "1"}) as client:
        assert await client.tools.call("say_hello", {}) == {
            "call_id": AnyStr(),
            "value": "Hello",
            "success": True,
        }
    async with get_async_test_client(app, headers={"x-api-key": "2"}) as client:
        # `2` does not have permission to call `say_hello`
        with pytest.raises(HTTPStatusError) as exception_info:
            assert await client.tools.call("say_hello", {})
        assert exception_info.value.response.status_code == 403

    async with get_async_test_client(app, headers={"x-api-key": "3"}) as client:
        # `3` does not have permission to call `say_hello`
        with pytest.raises(HTTPStatusError) as exception_info:
            assert await client.tools.call("say_hello", {})
        assert exception_info.value.response.status_code == 401


async def test_call_tool_with_injected() -> None:
    """Test calling a tool with an injected request."""
    app = Server()

    @app.add_tool(permissions=["authorized"])
    async def get_user_identity(request: Annotated[Request, InjectedRequest]) -> str:
        """Get the user's identity."""
        return request.user.identity

    auth = Auth()

    @auth.authenticate
    async def authenticate(headers: dict[bytes, bytes]) -> dict:
        """Authenticate incoming requests."""
        # Validate credentials (e.g., API key, JWT token)
        api_key = headers.get(b"x-api-key")

        api_key_to_user = {
            b"1": {"permissions": ["authorized"], "identity": "some-user"},
            b"2": {"permissions": ["authorized"], "identity": "another-user"},
            b"3": {"permissions": ["not-authorized"], "identity": "not-authorized"},
        }

        if not api_key or api_key not in api_key_to_user:
            raise auth.exceptions.HTTPException(detail="Not authorized")

        return api_key_to_user[api_key]

    app.add_auth(auth)

    async with get_async_test_client(app, headers={"x-api-key": "1"}) as client:
        result = await client.tools.call("get_user_identity")
        assert result["value"] == "some-user"

    async with get_async_test_client(app, headers={"x-api-key": "2"}) as client:
        result = await client.tools.call("get_user_identity")
        assert result["value"] == "another-user"

    async with get_async_test_client(app, headers={"x-api-key": "3"}) as client:
        with pytest.raises(HTTPStatusError) as exception_info:
            await client.tools.call("get_user_identity", {})
        assert exception_info.value.response.status_code == 403

    # Authenticated but tool does not exist
    async with get_async_test_client(app, headers={"x-api-key": "1"}) as client:
        with pytest.raises(HTTPStatusError) as exception_info:
            await client.tools.call("does_not_exist", {})
        assert exception_info.value.response.status_code == 403

    # Not authenticated
    async with get_async_test_client(app, headers={"x-api-key": "6"}) as client:
        # Make sure this raises 401?
        with pytest.raises(HTTPStatusError) as exception_info:
            await client.tools.call("does_not_exist", {})

        assert exception_info.value.response.status_code == 401


async def test_exposing_existing_langchain_tools() -> None:
    """Test exposing existing langchain tools."""
    from langchain_core.tools import StructuredTool, tool

    @tool
    def say_hello_sync() -> str:
        """Say hello."""
        return "Hello"

    @tool
    async def say_hello_async() -> str:
        """Say hello."""
        return "Hello"

    def multiply(a: int, b: int) -> int:
        """Multiply two numbers."""
        return a * b

    async def amultiply(a: int, b: int) -> int:
        """Multiply two numbers."""
        return a * b

    calculator = StructuredTool.from_function(func=multiply, coroutine=amultiply)

    server = Server()
    auth = Auth()
    server.add_auth(auth)

    @auth.authenticate
    async def authenticate(headers: dict) -> dict:
        """Authenticate incoming requests."""
        api_key = headers.get(b"x-api-key")

        api_key_to_user = {
            b"1": {"permissions": ["group1"], "identity": "some-user"},
        }

        if not api_key or api_key not in api_key_to_user:
            raise auth.exceptions.HTTPException(detail="Not authorized")

        return api_key_to_user[api_key]

    server.add_tool(say_hello_sync, permissions=["group1"])
    server.add_tool(say_hello_async, permissions=["group1"])
    server.add_tool(calculator, permissions=["group1"])

    async with get_async_test_client(server, headers={"x-api-key": "1"}) as client:
        tools = await client.tools.list()
        assert tools == [
            {
                "description": "Say hello.",
                "id": "say_hello_sync@1.0.0",
                "input_schema": {"properties": {}, "type": "object"},
                "name": "say_hello_sync",
                "output_schema": {"type": "string"},
                "version": "1.0.0",
            },
            {
                "description": "Say hello.",
                "id": "say_hello_async@1.0.0",
                "input_schema": {"properties": {}, "type": "object"},
                "name": "say_hello_async",
                "output_schema": {"type": "string"},
                "version": "1.0.0",
            },
            {
                "description": "Multiply two numbers.",
                "id": "multiply@1.0.0",
                "input_schema": {
                    "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                    "required": ["a", "b"],
                    "type": "object",
                },
                "name": "multiply",
                "output_schema": {"type": "integer"},
                "version": "1.0.0",
            },
        ]

        result = await client.tools.call("say_hello_sync", {})
        assert result == {
            "call_id": AnyStr(),
            "value": "Hello",
            "success": True,
        }

        result = await client.tools.call("say_hello_async", {})
        assert result == {
            "call_id": AnyStr(),
            "value": "Hello",
            "success": True,
        }

        result = await client.tools.call("multiply", {"a": 2, "b": 3})
        assert result == {
            "call_id": AnyStr(),
            "value": 6,
            "success": True,
        }


async def test_call_tool_by_version() -> None:
    """Test calling a tool by version."""
    app = Server()

    @app.add_tool(version=1)
    async def say_hello() -> str:
        """Say hello."""
        return "v1"

    @app.add_tool(version="2.0.0")
    async def say_hello() -> str:  # noqa: F811
        """Say hello."""
        return "v2"

    async with get_async_test_client(app) as client:
        tools = await client.tools.list()
        assert tools == [
            {
                "description": "Say hello.",
                "id": "say_hello@1.0.0",
                "input_schema": {"properties": {}, "type": "object"},
                "name": "say_hello",
                "output_schema": {"type": "string"},
                "version": "1.0.0",
            },
            {
                "description": "Say hello.",
                "id": "say_hello@2.0.0",
                "input_schema": {"properties": {}, "type": "object"},
                "name": "say_hello",
                "output_schema": {"type": "string"},
                "version": "2.0.0",
            },
        ]

        # call the tool by version
        result = await client.tools.call("say_hello@1", {})
        assert result == {
            "call_id": AnyStr(),
            "value": "v1",
            "success": True,
        }

        result = await client.tools.call("say_hello@1.0.0", {})
        assert result == {
            "call_id": AnyStr(),
            "value": "v1",
            "success": True,
        }

        result = await client.tools.call("say_hello@2", {})
        assert result == {
            "call_id": AnyStr(),
            "value": "v2",
            "success": True,
        }

        result = await client.tools.call("say_hello@2.0", {})
        assert result == {
            "call_id": AnyStr(),
            "value": "v2",
            "success": True,
        }

        result = await client.tools.call("say_hello", {})
        assert result == {
            "call_id": AnyStr(),
            "value": "v2",
            "success": True,
        }
