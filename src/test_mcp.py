import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main():
    async with streamable_http_client(
        "http://localhost:8000/mcp"
    ) as (read, write):

        async with ClientSession(read, write) as session:

            await session.initialize()

            print("MCP HTTP connected")
            print()

            tools = await session.list_tools()

            print("Tools:")
            for tool in tools.tools:
                print("-", tool.name)

            result = await session.call_tool(
                "get_spam_status",
                {}
            )

            print()
            print("Spam status:")
            print(result)


if __name__ == "__main__":
    asyncio.run(main)
