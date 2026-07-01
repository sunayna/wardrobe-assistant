from langchain_ollama import ChatOllama
from langchain_core.tools import tool
from langchain.agents import create_agent


@tool
def multiply(a: float, b: float) -> float:
    """Multiply two numbers together."""
    return a * b


@tool
def add(a: float, b: float) -> float:
    """Add two numbers together."""
    return a + b


tools = [multiply, add]

llm = ChatOllama(model="llama3.2", temperature=0)

agent = create_agent(llm, tools)


if __name__ == "__main__":
    result = agent.invoke({"messages": [{"role": "user", "content": "What is 3 multiplied by 7, then add 5?"}]})
    print("\nFinal answer:", result["messages"][-1].content)
