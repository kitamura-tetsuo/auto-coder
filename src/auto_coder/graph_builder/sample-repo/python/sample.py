"""
Sample Python file for testing
"""

from typing import Optional, Dict
import re


class User:
    """User class"""
    
    def __init__(self, user_id: str, name: str, email: str):
        """Initialize user"""
        self.id = user_id
        self.name = name
        self.email = email


class UserService:
    """User service class"""
    
    def __init__(self):
        """Initialize user service"""
        self.users: Dict[str, User] = {}
    
    def get_user_by_id(self, user_id: str) -> Optional[User]:
        """Get user by ID"""
        return self.users.get(user_id)
    
    def create_user(self, user: User) -> None:
        """Create a new user"""
        self.users[user.id] = user
    
    def validate_email(self, email: str) -> bool:
        """Validate user email"""
        email_regex = r'^[^\s@]+@[^\s@]+\.[^\s@]+$'
        return bool(re.match(email_regex, email))


async def fetch_user_from_api(user_id: str) -> User:
    """Fetch user from API"""
    # Simulated API call
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(f'/api/users/{user_id}') as response:
            data = await response.json()
            return User(**data)


def calculate_age(birth_year: int) -> int:
    """Calculate user age"""
    from datetime import datetime
    current_year = datetime.now().year
    return current_year - birth_year

