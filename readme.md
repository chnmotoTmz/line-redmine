# Line-Redmine Integration

A Python application that integrates Google Gemini AI with Redmine, enabling automated ticket creation through natural language processing and various data sources.

## Features

- **AI-Powered Ticket Creation**: Uses Google Gemini AI to process natural language prompts and create contextual Redmine tickets
- **Multiple Integration Methods**:
  - Direct Redmine REST API integration
  - MCP (Model Context Protocol) integration via Claude Desktop
- **Smart Ticket Processing**:
  - Contextual priority setting based on input data
  - Automated description generation
  - Customizable ticket fields
- **Environment Configuration**: Secure configuration management using `.env` files
- **Comprehensive Logging**: Detailed activity logging in `application.log`

## Requirements

- Python 3.8+
- Redmine 5.0.12+ with REST API enabled
- Google Gemini API key
- Required Python packages (see `requirements.txt`)
- Optional: Claude Desktop with MCP for advanced integration

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/your-repo/line-redmine.git
   cd line-redmine
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Configure environment variables in `.env`:
   ```env
   GOOGLE_API_KEY="your_gemini_api_key"
   REDMINE_URL="http://your-redmine-url"
   REDMINE_API_KEY="your_redmine_api_key"
   REDMINE_PROJECT_ID="your_project_id"
   MCP_URL="http://localhost:8000/mcp-redmine"  # Optional for MCP integration
   ```

## Usage

### Basic Usage
```bash
python main.py "Your ticket description"
```

### Example Commands
```bash
# Create a basic ticket
python main.py "Create a high-priority task for server maintenance"

# Create a weather-based ticket
python main.py "Create ticket if Boston weather requires maintenance"
```

## Integration Methods

### Direct REST API Integration
- Direct communication with Redmine's REST API
- Faster response times (typically <500ms)
- Ideal for simple ticket creation workflows

### MCP Integration (Optional)
- Enhanced AI processing through Claude Desktop
- Support for complex workflows and multiple tool integration
- Requires additional setup of Claude Desktop and MCP

## Configuration

### Required Environment Variables
- `GOOGLE_API_KEY`: Google Gemini API authentication
- `REDMINE_URL`: Your Redmine instance URL
- `REDMINE_API_KEY`: Redmine API authentication
- `REDMINE_PROJECT_ID`: Target project for ticket creation

### Optional Environment Variables
- `MCP_URL`: MCP server endpoint (for Claude Desktop integration)
- `LOG_LEVEL`: Logging detail level (default: INFO)

## Error Handling

The application includes comprehensive error handling for:
- API authentication failures
- Network connectivity issues
- Invalid input data
- MCP server connection problems

## Logging

Detailed logs are maintained in `application.log`, including:
- API requests and responses
- Ticket creation details
- Error messages and warnings
- Performance metrics

## Development

### Adding New Features
1. Fork the repository
2. Create a feature branch
3. Submit a pull request

### Running Tests
```bash
python -m pytest tests/
```

## Performance

- REST API Response Time: <500ms
- MCP Integration Response Time: <1s
- Supports concurrent ticket creation

## Security

- API keys stored securely in environment variables
- HTTPS required for all API communications
- Input validation and sanitization
- Secure error logging

## Support

For issues and feature requests:
1. Check existing GitHub issues
2. Open a new issue if needed
3. Include logs and environment details

## License

This project is licensed under the MIT License - see the LICENSE file for details.# line-redmine
# line-redmine
