import sys
import logging
import os
import time
import json
import requests
import datetime
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from flask import Flask, request
from azure.cosmos import exceptions, CosmosClient, PartitionKey
from questions_payloads import *
from statistics import update_statistics

###############################################################################
# Initializing
###############################################################################
# initializing survey_dict
survey_dict = {}
psych_dict = {}

# enable logging
logging.basicConfig(level=logging.DEBUG)

# Initialize bolt
bolt_app = App(
    token=os.environ.get("SLACK_BOT_TOKEN"),
    signing_secret=os.environ.get("SLACK_SIGNING_SECRET")
)

# Initialize Flask app and bolt handler
app = Flask(__name__)
handler = SlackRequestHandler(bolt_app)

# redirect request to bolt
@app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)

## Start platform related code
# Create a logger for the 'azure' SDK and configure a console output
azureLogger = logging.getLogger('azure')
azureLogger.setLevel(logging.DEBUG)
azureLogger.addHandler(logging.StreamHandler(stream=sys.stdout))

# Initialize the Cosmos client
cosmos = CosmosClient(
    url=os.environ.get("AZURE_COSMOS_ENDPOINT"),
    credential=os.environ.get("AZURE_COSMOS_MASTER_KEY"),
    logging_enable=True
)

# Create a database
database_name = 'bot-storage'
database = cosmos.create_database_if_not_exists(id=database_name)

# Create a container
# Using a good partition key improves the performance of database operations.
msgDB_name = 'message-storage'
msgDB = database.create_container_if_not_exists(
    id=msgDB_name,
    partition_key=PartitionKey(path="/user"),
    offer_throughput=400
)
survey_containter = database.get_container_client("survey-storage")
## Add more container here for survey

#Create Brainstorming container
brainDB_name = 'brainstorm-storage'
brainDB = database.create_container_if_not_exists(
    id=brainDB_name,
    partition_key=PartitionKey(path="/user"),
    offer_throughput=400
)

# Create container for statistics.
statDB_name = 'statistics-storage'
statDB = database.create_container_if_not_exists(
    id=statDB_name,
    partition_key=PartitionKey(path="/info_type")
)

#Create Psych container
psychDB_name = 'psych-storage'
psychDB = database.create_container_if_not_exists(
    id=psychDB_name,
    partition_key=PartitionKey(path="/user"),
    offer_throughput=400
)

# Insert the initial item for workspace-wide statistics only if it doesn't exist:
try:
    statDB.create_item({
        'id': '1',
        'total_workspace_messages': 0,
        'total_users': 0,
        'average_msg_time': 0,
        'sum_msg_ts': 0,
        'info_type': 'Workspace-wide stats'
    }
)

except exceptions.CosmosHttpResponseError:
    print("Initial item for workspace-wide statistics already exists, continuing:")

try:
    statDB.create_item({
        'id': '2',
        'Feedback-Change': 0,
        "Feedback-Keep": 0,
        'Psych-Completed': 0,
        'info_type': 'Survey stats'
    }
)

except exceptions.CosmosHttpResponseError:
    print("Initial item for workspace-wide statistics already exists, continuing:")

# Insert the initial items for individual user statistics.
user_result = bolt_app.client.users_list()
for user in user_result["members"]:
    try:
        statDB.create_item({
            'id': user["id"],
            'total_user_messages': 0,
            'total_sent_mentions': 0,
            'total_received_mentions': 0,
            'total_long_user_messages': 0,
            'total_short_user_messages': 0,
            'psychScore': 0,
            'previous_messages': 0,
            'most_messages': 0,
            'sentiment_count': 0,
            'sentiment_score': 0,
            'info_type': 'User stats'
        }
        )

    except exceptions.CosmosHttpResponseError:
        print("Initial item for user:", user["id"], "statistics already exists, continuing:")

# Insert the initial items for individual channel statistics.
channel_result = bolt_app.client.conversations_list()
for channel in channel_result["channels"]:
    try:
        statDB.create_item({
            'id': channel["id"],
            'total_channel_messages': 0,
            'total_long_channel_messages': 0,
            'total_short_channel_messages': 0,
            'info_type': 'Channel stats'
        }
        )

    except exceptions.CosmosHttpResponseError:
        print("Initial item for channel:", channel["id"], "statistics already exists, continuing:")

## The database usage in the rest part may need to be changed on a different platform

# API setup for sentiment analysis
subscription_key=os.environ.get("TEXT_ANALYTICS_KEY")
endpoint=os.environ.get("TEXT_ANALYTICS_ENDPOINT")
language_api_url = endpoint + "/text/analytics/v3.0/languages"
sentiment_url = endpoint + "/text/analytics/v3.0/sentiment"

## End platform related code

# Global Variables

#Brainstorming Globals
brainstormOn = 0
brain_weekly = 0

#Weekly Survey 
weeklySurveyValue = 0
weekly_id = ""
channel = ""
weeklyCompleted = 0
psychBad = 0

###############################################################################
# Middleware
###############################################################################

# Log and print request
@bolt_app.middleware
def log_request(logger, body, next):
    logger.debug(body)
    return next()

# Log all messages
@bolt_app.middleware
def log_message(client, payload, next):
    global brainstormOn

    if ("type" in payload and payload["type"]=="message"):
        text = payload["text"]
        # get mention
        mentions = []
        for i in range(len(text)):
            if text[i] == "<":
                if text[i+1] == "@": 
                    j = i + 1
                    while text[j] != ">":
                        j = j + 1
                    mentions.append(text[i+2:j])
        # sentiment analysis
        # get language
        lang_documents = {"documents": [{
            "id": payload["ts"], 
            "text": payload["text"]}
        ]}
        headers = {"Ocp-Apim-Subscription-Key": subscription_key}
        lang_response = requests.post(language_api_url, headers=headers, json=lang_documents)
        # wait for response
        time.sleep(1)
        languages = lang_response.json()
        print(languages["documents"])

        senti_documents = {"documents": [{
            "id": payload["ts"], 
            "language": languages["documents"][0]["detectedLanguage"]["iso6391Name"],
            "text": payload["text"]}
        ]}
        response = requests.post(sentiment_url, headers=headers, json=senti_documents)
        time.sleep(1)
        sentiments = response.json()
        if (sentiments != None):
            sentiment = sentiments["documents"][0]["confidenceScores"]["positive"] - sentiments["documents"][0]["confidenceScores"]["negative"]
        else:
            sentiment = None
        # id is required
        msg = {
            'id' : payload["ts"],
            'channel': payload["channel"],
            'user': payload["user"],
            'message': payload["text"],
            'mention': mentions,
            'sentiment': sentiment
        }
        msgDB.upsert_item(msg)
        # update_statistics(msg, statDB)    # this line causes a ModuleNotFoundError with slack_bolt for unknown reasons.
        # due to the above error, im trying just having the code here for now.
        # Also, the updating definitely should be condensed into a function, but Ill probably do that later

        # bad sentiment alert
        if (sentiment < -0.5):
            client.chat_postMessage(channel=payload['user'], text=f"Hey there <@{payload['user']}>, be careful! You are saying very bad words!")

        # Update workspace-wide statistics
        prev_workspace_stats = statDB.read_item(item="1", partition_key="Workspace-wide stats")
        prev_workspace_stats['total_workspace_messages'] += 1
        # statDB.replace_item("1", prev_workspace_stats)

        # prev_workspace_stats = statDB.read_item(item="1", partition_key="Workspace-wide stats")
        msg_ts = float(payload["ts"])
        date = datetime.datetime.fromtimestamp(msg_ts)
        msg_ts_time = date.time()

        msg_ts_secs = int(msg_ts_time.hour) * 3600 + int(msg_ts_time.minute) * 60 + int(msg_ts_time.second)
        prev_workspace_stats['sum_msg_ts'] += msg_ts_secs
        new_avg = int(prev_workspace_stats['sum_msg_ts']) / int(prev_workspace_stats["total_workspace_messages"]+1)

        prev_workspace_stats['average_msg_time'] = str(datetime.timedelta(seconds=new_avg))
        statDB.replace_item("1", prev_workspace_stats)

        # Update individual user statistics

        # Total messages sent
        prev_user_stats = statDB.read_item(item=payload["user"], partition_key="User stats")
        prev_user_stats['total_user_messages'] += 1
        # statDB.replace_item(payload["user"], prev_user_stats)

        # Message length
        if len(payload["text"]) > 40:
            prev_user_stats = statDB.read_item(item=payload["user"], partition_key="User stats")
            prev_user_stats['total_long_user_messages'] += 1
            # statDB.replace_item(payload["user"], prev_user_stats)
        else:
            prev_user_stats = statDB.read_item(item=payload["user"], partition_key="User stats")
            prev_user_stats['total_short_user_messages'] += 1
            # statDB.replace_item(payload["user"], prev_user_stats)

        # Per user sentiment
        prev_user_stats = statDB.read_item(item=payload["user"], partition_key="User stats")
        count = prev_user_stats['sentiment_count']
        prev_user_stats['sentiment_score'] = (prev_user_stats['sentiment_score']*count + sentiment)/(count+1)
        prev_user_stats['sentiment_count'] = count + 1
        # statDB.replace_item(payload["user"], prev_user_stats)

        # Check and record mentions
        for user in user_result["members"]:
            if user in mentions:
                prev_user_stats = statDB.read_item(item=user, partition_key="User stats")
                prev_user_stats['total_received_mentions'] += 1
                statDB.replace_item(user, prev_user_stats)

                prev_user_stats = statDB.read_item(item=payload["user"], partition_key="User stats")
                prev_user_stats['total_sent_mentions'] += 1
                # statDB.replace_item(payload["user"], prev_user_stats)

        # update database
        statDB.replace_item(payload["user"], prev_user_stats)

        # Update individual channel statistics

        # Total messages sent and message length
        try:
            prev_channel_stats = statDB.read_item(item=payload["channel"], partition_key="Channel stats")
            prev_channel_stats['total_channel_messages'] += 1
            if len(payload["text"]) > 40:
                prev_channel_stats['total_long_channel_messages'] += 1
            else:
                prev_channel_stats['total_short_channel_messages'] += 1
            statDB.replace_item(payload["channel"], prev_channel_stats)
        except exceptions.CosmosHttpResponseError:
            print("Channel:", payload["channel"], "not found, continuing:")

        # Brainstorming
        if (brainstormOn == 1):
            msgBrain = {
                'id' : payload["ts"],
                'channel': payload["channel"],
                'user': payload["user"],
                'message': payload["text"],
                'mention': None
            }
            brainDB.create_item(msgBrain)

    return next()

###############################################################################
# Message Handler
###############################################################################

# Listens to incoming messages that contain "hello"
@bolt_app.message("hello")
def message_hello(ack, message, say):
    # say() sends a message to the channel where the event was triggered
    ack()
    say(
        blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"Hey there <@{message['user']}>!"},
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Click Me"},
                    "action_id": "button_click"
                }
            }
        ],
        text=f"Hey there <@{message['user']}>!"
    )


    #returns true if user is an introvert, false otherwise
def is_introvert(user):
    temp = survey_dict[user]
    e = 20 + temp[0] - temp[5] + temp[11] - temp[15] + temp[20] - temp[25] + temp[30] - temp[35] + temp[40] - temp[45]
    if e < 13:
        return True
    else:
        return False

#returns true if user is an extrovert, false otherwise
def is_extrovert(user):
    temp = survey_dict[user]
    e = 20 + temp[0] - temp[5] + temp[11] - temp[15] + temp[20] - temp[25] + temp[30] - temp[35] + temp[40] - temp[45]
    if e > 27:
        return True
    else:
        return False

# handle all messages
@bolt_app.message("")
def message_rest(ack, client, message):
    ack()
    workspace_stats = statDB.read_item(item="1", partition_key="Workspace-wide stats")
    user_result = client.users_list()
    user_result = user_result['members']
    user_count = len(user_result)
    average = workspace_stats['total_workspace_messages']/user_count
    most_messages = 0
    user_with_most = message['user']
    if workspace_stats['total_workspace_messages'] % 100 == 0:
        total = total/(workspace_stats['total_workspace_messages']/100)
        for user in user_result:
            
            user_stats = statDB.read_item(item=user["id"], partition_key="User stats")
            total = user_stats['total_user_messages'] - user_stats['previous_messages']
            
            user_stats['previous_messages'] = user_stats['total_user_messages']
            # statDB.replace_item(payload["user"], user_stats)
            if (total < average - 15) and (is_introvert(user['id'])):
                client.chat_postMessage(channel=user['id'], text=f"Hey there <@{user['id']}>, I have noticed you haven't been contributing a lot recently. We would love to hear your ideas!")
            elif (total > average + 20) and (is_extrovert(user['id'])):
                client.chat_postMessage(channel=user['id'], text=f"Hey there <@{user['id']}>, I have noticed you have been sending a lot of messages recently. Just wanted to check in and make sure that everyone has had the opportunity to share their ideas!")
            # sentiment alert
            if (user_stats['sentiment_score'] < 0):
                client.chat_postMessage(channel=user['id'], text=f"Hey there <@{user['id']}>, I have noticed you aren't communicating in a friendly way. Please be kind to your teammates!")
            user_stats['sentiment_count'] = 0
            statDB.replace_item(user["id"], user_stats)


###############################################################################
# Action Handler
###############################################################################
# handler for a radio button being selected
@bolt_app.action("this_is_an_action_id")
def action_button_click(ack, body, client, say):
    # Acknowledge the action
    ack()
    user = body['user']['id']
    form_json = json.dumps(body)
    form_json = form_json[-150:]
    question_number = ""
    answer = ""
    result = form_json.find('value')
    form_json = form_json[result+10:]
    for x in range(len(form_json)):
        if form_json[x] == '_':
            answer = form_json[x+1]
            break
        else:
            question_number += form_json[x]
    answer = int(answer)
    question_number = int(question_number)
    temp = survey_dict[user]
    temp[question_number-1] = answer
    survey_dict[user] = temp
                
                

@bolt_app.action("psych_radio_id")
def action_button_click(ack, body, say):
    ack()
    global psychBad
    form_json = json.dumps(body)
    form_json = form_json[500:]
    actions_index = form_json.find('actions')
    form_json = form_json[actions_index:]
    value_index = form_json.find('value')
    value = form_json[value_index+9]
    user = body['user']['id']

    psychScore = statDB.read_item(item=user, partition_key="User stats")
    psychScore['psychScore'] += int(value)

    if (form_json[value_index+11] == "7"):
        psychScore['psychScore'] /= 7
        if (psychScore['psychScore'] < 3):
            psychBad = 1
        psychScore['psychScore'] = 0

    statDB.replace_item(user, psychScore)

    

    
    
@bolt_app.action("EndBrainstorming")
def action_button_click(ack, body, say):
    # Acknowledge the action
    ack()
    global brainstormOn
    global brain_weekly
    #Check if brainstorm bit is already 0 to prevent spamming of the button
    if (brainstormOn == 1):
        brainstormOn = 0
        say('Here are all of the ideas the group came up with: ')

        #iterate through all of the ideas the group proposed
        item_list = list(brainDB.read_all_items())
        msg = ""
        for i in item_list:
            msg += "• " + i.get("message") + "\n"
            brainDB.delete_item(item = i.get("id"), partition_key = i.get("user"))
        say(msg)
        say("Need a mockup of one of the ideas? Try using <https://www.sketchup.com/plans-and-pricing/sketchup-free|Google Sketch up> or <https://www.figma.com/|Figma>")
        if (brain_weekly == 1):
            say("Also a reminder has been set for next week to look back on the brainstorming process")      
    else:
        say("Brainstorming has already ended")

@bolt_app.action("button_click")
def action_button_click(ack, body, say):
    # Acknowledge the action
    ack()
    say(f"<@{body['user']['id']}> clicked the button")

@bolt_app.action("take_survey")
def action_button_click(ack, body, client):
    # Acknowledge the action
    user = body['user']['id']
    survey_dict[user] = [0 for x in range(50)]
    ack()
    client.views_open(
        # Pass a valid trigger_id within 3 seconds of receiving it
            trigger_id=body["trigger_id"],
        # View payload
            view=question1_payload
    )

@bolt_app.action("back")
def action_button_click(ack, body, client):
    ack()
    form_json = json.dumps(body)
    result = form_json.find('Question')
    form_json = form_json[result+8:]
    question_number = ""
    for char in form_json:
        if char == '"':
            break
        else:
            question_number += char
    question_number = int(question_number)
            
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question_list[question_number-2]
    )

@bolt_app.action("question1_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question2_payload
    )
    
@bolt_app.action("question2_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question3_payload
    )


@bolt_app.action("question3_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question4_payload
    )
    
@bolt_app.action("question4_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question5_payload
    )
    
@bolt_app.action("question5_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question6_payload
    )
    
@bolt_app.action("question6_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question7_payload
    )
    
@bolt_app.action("question7_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question8_payload
    )
    
@bolt_app.action("question8_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question9_payload
    )
    
@bolt_app.action("question9_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question10_payload
    )
    
@bolt_app.action("question10_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question11_payload
    )
    
@bolt_app.action("question11_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question12_payload
    )
    
@bolt_app.action("question12_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question13_payload
    )
    
@bolt_app.action("question13_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question14_payload
    )
    
@bolt_app.action("question14_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question15_payload
    )
    
@bolt_app.action("question15_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question16_payload
    )
    
@bolt_app.action("question16_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question17_payload
    )
    
@bolt_app.action("question17_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question18_payload
    )
    
@bolt_app.action("question18_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question19_payload
    )
    
@bolt_app.action("question19_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question20_payload
    )
    
@bolt_app.action("question20_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question21_payload
    )
    
@bolt_app.action("question21_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question22_payload
    )
    
@bolt_app.action("question22_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question23_payload
    )
    
@bolt_app.action("question23_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question24_payload
    )
    
@bolt_app.action("question24_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question25_payload
    )
    
@bolt_app.action("question25_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question26_payload
    )
    
@bolt_app.action("question26_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question27_payload
    )
    
@bolt_app.action("question27_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question28_payload
    )
    
@bolt_app.action("question28_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question29_payload
    )
    
@bolt_app.action("question29_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question30_payload
    )
    
@bolt_app.action("question30_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question31_payload
    )
    
@bolt_app.action("question31_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question32_payload
    )
    
@bolt_app.action("question32_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question33_payload
    )
    
@bolt_app.action("question33_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question34_payload
    )
    
@bolt_app.action("question34_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question35_payload
    )
    
@bolt_app.action("question35_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question36_payload
    )
    
@bolt_app.action("question36_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question37_payload
    )
    
@bolt_app.action("question37_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question38_payload
    )
    
@bolt_app.action("question38_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question39_payload
    )
    
@bolt_app.action("question39_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question40_payload
    )


@bolt_app.action("question40_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question41_payload
    )
    
@bolt_app.action("question41_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question42_payload
    )
    
@bolt_app.action("question42_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question43_payload
    )
    
@bolt_app.action("question43_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question44_payload
    )
    
@bolt_app.action("question44_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question45_payload
    )
    
@bolt_app.action("question45_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question46_payload
    )
    
@bolt_app.action("question46_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question47_payload
    )
    
@bolt_app.action("question47_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question48_payload
    )
    
@bolt_app.action("question48_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question49_payload
    )
    
@bolt_app.action("question49_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=question50_payload
    )
    
@bolt_app.action("submit")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    user = body["user"]["id"]
    temp = survey_dict[user]
    e = 20 + temp[0] - temp[5] + temp[11] - temp[15] + temp[20] - temp[25] + temp[30] - temp[35] + temp[40] - temp[45]
    a = 14 - temp[1] + temp[6] - temp[11] + temp[16] - temp[21] + temp[26] - temp[31] + temp[36] + temp[41] + temp[46]
    c = 14 + temp[2] - temp[7] + temp[12] - temp[17] + temp[22] - temp[27] + temp[32] - temp[37] + temp[42] + temp[47]
    n = 38 - temp[3] + temp[8] - temp[13] + temp[18] - temp[23] - temp[28] - temp[33] - temp[38] - temp[43] - temp[48]
    o = 8 + temp[4] - temp[9] + temp[14] - temp[19] + temp[24] - temp[29] + temp[34] + temp[39] + temp[44] + temp[49]
    text = "E %d A %d C %d N %d O %d" % (e,a,c,n,o)
    client.views_update(
               view_id=body["view"]["id"],
           # Pass a valid trigger_id within 3 seconds of receiving it
               hash=body["view"]["hash"],
           # View payload
               view={
                   "type": "modal",
               # View identifier
                   "callback_id": "view_1",
                   "title": {"type": "plain_text", "text": "Results"},
                   
                   "blocks": [
                       {
                           "type": "section",
                           "text": {"type": "mrkdwn", "text": "Each Score is between 0 and 40, 0 being you don't embody this trait at all and 40 being you totally embody the trait"}
                           
                       },
                       {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": "Extroversion is the personality trait of seeking fulfillment from sources outside the self or in community. High scorers tend to be very social while low scorers prefer to work on their projects alone. Your score for Extroversion is %d" % (e)}
                       
                       
                              },
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": "Agreeableness reflects much individuals adjust their behavior to suit others. High scorers are typically polite and like people. Low scorers tend to 'tell it like it is'. Your score for Agreeableness is %d" % (a)}
                                  
                        },
                       {
                           "type": "section",
                           "text": {"type": "mrkdwn", "text": "Conscientiousness is the personality trait of being honest and hardworking. High scorers tend to follow rules and prefer clean homes. Low scorers may be messy and cheat others. Your score for Conscientiousness is %d" % (c)}
                                 
                       },
                       {
                           "type": "section",
                           "text": {"type": "mrkdwn", "text": "Neuroticism is the personality trait of being emotional. Your score for Neuroticism is %d" % (n)}
                                 
                       },
                       {
                           "type": "section",
                           "text": {"type": "mrkdwn", "text": "Openness to Experience is the personality trait of seeking new experience and intellectual pursuits. High scores may day dream a lot. Low scorers may be very down to earth. Your score for Openness to Experience is %d" % (o)}
                                 
                       }
                       
                   ]
               }
       )
    

# Psych Survey
        
@bolt_app.action("psych_q1_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=psych_q2_payload
    )

@bolt_app.action("psych_q2_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=psych_q3_payload
    )

@bolt_app.action("psych_q3_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=psych_q4_payload
    )

@bolt_app.action("psych_q4_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=psych_q5_payload
    )

@bolt_app.action("psych_q5_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=psych_q6_payload
    )

@bolt_app.action("psych_q6_next")
def action_button_click(ack, body, client):
    # Acknowledge the action
    ack()
    client.views_update(
            view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
            hash=body["view"]["hash"],
        # View payload
            view=psych_q7_payload
    )

@bolt_app.action("psych_submit")
def action_button_click(ack, body, client, say):
    # Acknowledge the action
    ack()
    global channel
    global weeklyCompleted
    global psychBad
    user = body["user"]["id"]

    prev_psych_stats = statDB.read_item(item="2", partition_key="Survey stats")
    prev_psych_stats['Psych-Completed'] += 1

    totalMembers = statDB.read_item(item="1", partition_key="Workspace-wide stats")

    if(psychBad == 1 and prev_psych_stats['Psych-Completed'] == totalMembers['total_users']):
        client.chat_postMessage(channel = channel, text = 'Thank you all for taking the survey, at least 1 member identified that they feel the team enviroment does not feel psychologically safe. Please be more open to opinions and speak respectfully to each other.')
        psychBad = 0

    if(prev_psych_stats['Psych-Completed'] == totalMembers['total_users']):
        prev_psych_stats['Psych-Completed'] = 0
    statDB.replace_item("2", prev_psych_stats)
    weeklyCompleted = prev_psych_stats['Psych-Completed'];

    client.views_update(
        view_id=body["view"]["id"],
        # Pass a valid trigger_id within 3 seconds of receiving it
        hash=body["view"]["hash"],
        # View payload
        view={
            "type": "modal",
            "callback_id": "view_1",
            "title": {"type": "plain_text", "text": "Thank You"},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "plain_text",
                        "text": "Your survey has been submitted. Thank you."
                    }
                }
            ]
        }
    )

    client.chat_postEphemeral(
        channel = channel,
        user = user,
        text = "Thank you for taking the survey! Do you think the surveys is asked too frequently or just right?",
        blocks = [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "Thank you for taking the survey! Do you think the surveys is asked too frequently or just right? \n Please Select an Option"},
                    "accessory": {
                        "type": "radio_buttons",
                        "action_id": "psychFeedback",
                
                        "options": [
                        {
                            "value": "0",
                            "text": {
                            "type": "plain_text",
                            "text": "Perfect"
                            }   
                        },
                        {
                            "value": "1",
                            "text": {
                            "type": "plain_text",
                            "text": "Too Frequent"
                            }
                        }]
                    }
                }]
    )



@bolt_app.action("psychFeedback")
def psych_feedback(ack, body, client, say):
    # Acknowledge the action
    ack()
    form_json = json.dumps(body)
    form_json = form_json[500:]
    actions_index = form_json.find('actions')
    form_json = form_json[actions_index:]
    value_index = form_json.find('value')
    value = form_json[value_index+9]

    prev_psych_stats = statDB.read_item(item="2", partition_key="Survey stats")

    if (value == '1'):
        prev_psych_stats['Feedback-Change'] += 1
    else:
        prev_psych_stats['Feedback-Keep'] += 1

    totalFeedback = prev_psych_stats['Feedback-Change'] + prev_psych_stats['Feedback-Keep']
    totalMembers = statDB.read_item(item="1", partition_key="Workspace-wide stats")
    if (totalFeedback == totalMembers['total_users']):
        ratio = prev_psych_stats['Feedback-Change']/totalMembers['total_users']
        prev_psych_stats['Feedback-Change'] = 0
        prev_psych_stats['Feedback-Keep'] = 0
        if (ratio >= .5):
            say("Thank you all for your feedback on the psych survey! It appears most members feel the survey is too frequent. Perhaps consider changing its frequency on the dashboard.")
    statDB.replace_item("2", prev_psych_stats)
    
    


###############################################################################
# Event Handler
###############################################################################
@bolt_app.event("view_closed")
def action_button_click(ack, event, say):
    # Acknowledge the action
    user = event["user"]["id"]
    temp = survey_dict[user]
    e = 20 + temp[0] - temp[5] + temp[11] - temp[15] + temp[20] - temp[25] + temp[30] - temp[35] + temp[40] - temp[45]
    a = 14 - temp[1] + temp[6] - temp[11] + temp[16] - temp[21] + temp[26] - temp[31] + temp[36] + temp[41] + temp[46]
    c = 14 + temp[2] - temp[7] + temp[12] - temp[17] + temp[22] - temp[27] + temp[32] - temp[37] + temp[42] + temp[47]
    n = 38 - temp[3] + temp[8] - temp[13] + temp[18] - temp[23] - temp[28] - temp[33] - temp[38] - temp[43] - temp[48]
    o = 8 + temp[4] - temp[9] + temp[14] - temp[19] + temp[24] - temp[29] + temp[34] + temp[39] + temp[44] + temp[49]
    say("E %d A %d C %d N %d O %d" % (e,a,c,n,o))
    ack()



# Example reaction emoji echo
@bolt_app.event("reaction_added")
def reaction_added(ack, event, say, client):
    ack()
    emoji = event["reaction"]
    channel = event["item"]["channel"]
    user = event["user"]
    ts = event["item"]["ts"]


# Triggering event upon new member joining
@bolt_app.event("member_joined_channel")
def new_member_survey(ack, event, say):
    global channel
    ack()
    prev_workspace_stats = statDB.read_item(item="1", partition_key="Workspace-wide stats")
    prev_workspace_stats['total_users'] += 1
    statDB.replace_item("1", prev_workspace_stats)

    user = event["user"]
    channel = event["channel"]
    message = "Hello <@%s> Thanks for joining the chat!, Please take a personality survey by pressing the take survey button! :tada:" % user
    say(
        blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": message},
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Take Survey"},
                    "action_id": "take_survey"
                }
            }
        ],
        text=message
    )
  
@bolt_app.event("member_left_channel")
def member_leaving(ack, event, say):
    global channel
    ack()
    prev_workspace_stats = statDB.read_item(item="1", partition_key="Workspace-wide stats")
    prev_workspace_stats['total_users'] -= 1
    statDB.replace_item("1", prev_workspace_stats)      

# Error events
@bolt_app.event("error")
def error_handler(ack, err):
    ack()
    print("ERROR: " + str(err))

###############################################################################
# Slash Command Handler
###############################################################################

# Sample slash command "/hello"
@bolt_app.command('/hello')
def hello(ack, say):
    # Acknowledge command request
    ack()
    # Send 'Hello!' to channel
    say('Hello!')


# The echo command simply echoes on command
@bolt_app.command('/echo')
def repeat_text(ack, say, command):
    # Acknowledge command request
    ack()
    say(f"{command['text']}")

# Sample slash command "/samplesurvey"
@bolt_app.command('/samplesurvey')
def sampleSurvey(ack, body, client, logger):
    ack()
    try:
        client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "title": {
                    "type": "plain_text",
                    "text": "Sample Servey",
                    "emoji": True
                },
                "submit": {
                    "type": "plain_text",
                    "text": "Submit",
                    "emoji": True
                },
                "close": {
                    "type": "plain_text",
                    "text": "Cancel",
                    "emoji": True
                },
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "Please select *True* _or_ *False*."
                        }
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "True",
                                    "emoji": True
                                },
                                "value": "True"
                            }
                        ]
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "False",
                                    "emoji": True
                                },
                                "value": "False"
                            }
                        ]
                    }
                ]
            }
        )
    except Exception as e:
        logger.error(f"Error opening modal: {e}")
        
        
#slash command for survey
@bolt_app.command('/survey')
def survey(ack, body, client):
# Acknowledge the command request
    ack()
# Call views_open with the built-in client
    client.views_open(
    # Pass a valid trigger_id within 3 seconds of receiving it
        trigger_id=body["trigger_id"],
    # View payload
        view={
            "type": "modal",
        # View identifier
            "callback_id": "view_1",
            "title": {"type": "plain_text", "text": "My App"},
            "submit": {"type": "plain_text", "text": "Submit"},
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "Welcome to a modal with _blocks_"},
                    "accessory": {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Click me!"},
                        "action_id": "button_abc"
                    }
                },
                {
                    "type": "input",
                    "block_id": "input_c",
                    "label": {"type": "plain_text", "text": "What are your hopes and dreams?"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "dreamy_input",
                        "multiline": True
                    }
                }
            ]
        }
)

# Psych Survey slash command (temp)
@bolt_app.command('/psych_survey')
def psych_survey(ack, body, client, say):
    global weeklySurveyValue
    global weekly_id
    global channel
    user = body['user_id']
    psych_dict[user] = [0 for x in range(8)]
    ack()

    if (weeklySurveyValue == 0):
        say("Please go to my Dashboard and set up the reminder before taking the survey!")
        return

    ts = time.time()
    ts = ts + (weeklySurveyValue*604800)
    try:
        client.chat_deleteScheduledMessage(channel = channel, scheduled_message_id = weekly_id)
        weekly_id = client.chat_scheduleMessage(
            channel = channel,
            text = "Please take your psychological saftey survey using /psych_survey",
            post_at = ts,
        )
    except:
        weekly_id = client.chat_scheduleMessage(
            channel = channel,
            text = "Please take your psychological saftey survey using /psych_survey",
            post_at = ts,
        ) 

    client.views_open(
        # Pass a valid trigger_id within 3 seconds of receiving it
            trigger_id=body["trigger_id"],
        # View payload
            view=psych_q1_payload
    )

@bolt_app.command('/startbrainstorming')
def start_brainstorming(ack, body, say, command, client):
    ack()
    global brainstormOn
    global brain_weekly
    #Set the global listening bit to 1 to open up the container
    brainstormOn = 1
    say('Brainstorm listening has begun! A 30 minute timer has started or you can manually end the listening by using: /EndBrainstorming. Remember do not critique ideas until after the session is over')
    
    channel = command["channel_id"]
    ts = time.time()
    
    #Schedule Reminders to the group throughout the process
    client.chat_scheduleMessage(
        channel = channel,
        text = "Reminder: Brainstorm listening ends in 15 minutes. Think outside the box and dont be afraid to come up with unique ideas!",
        post_at = ts + 900,
    )

    client.chat_scheduleMessage(
        channel = channel,
        text = "Brainstorm listening has ended",
        attachments =
            [
                {
                    "text": "Please hit this Button to End Brainstorming",
                    "fallback": "Error",
                    "callback_id": "EndBrainstorming",
                    "color": "#3AA3E3",
                    "actions": [
                        {
                            "name": "EndBrainstorming",
                            "text": "End Brainstorming",
                            "type": "button",
                            "value": "End"
                        }
                    ]
                }
            ],
        post_at = ts + 1800,
    )
    
    if (brain_weekly == 1):
        client.chat_scheduleMessage(
            channel = channel,
            text = "Reminder: Look back on the Brainstorming session you had last week, was an Idea decided upon? Perhaps more mockups or another brainstorming session is needed?",
            post_at = ts + 604800,
        )

@bolt_app.command('/endbrainstorming')
def end_brainstorming(ack, body, say, command, client):
    ack()
    global brainstormOn
    global brain_weekly
    #If brainstorming is off no need to run through the rest of the proceedures
    if (brainstormOn == 1):
        brainstormOn = 0
        say('Brainstorm listening has ended')
        channel = command["channel_id"]
        
        #Try checking if any of the scheduled messages still need to be run, if they do just delete them
        ts = time.time()
        scheduledList = client.chat_scheduledMessages_list(channel = channel, latest = ts + 1800, oldest = ts)
        for i in scheduledList['scheduled_messages']:
            try:
                client.chat_deleteScheduledMessage(channel = channel, scheduled_message_id = i["id"])
            except:
                pass

        #Iterate back to the group all of the ideas they came up with
        say('Here are all of the ideas the group came up with: ')
        item_list = list(brainDB.read_all_items())
        msg = ""
        for i in item_list:
            msg += "• " + i.get("message") + "\n"
            brainDB.delete_item(item = i.get("id"), partition_key = i.get("user"))
        say(msg)
        say("Need a mockup of one of the ideas? Try using <https://www.sketchup.com/plans-and-pricing/sketchup-free|Google Sketch up> or <https://www.figma.com/|Figma>")
        if (brain_weekly == 1):
            say("Also a reminder has been set for next week to look back on the brainstorming process")
    else:
        say("Brainstorming has already ended")
    

###############################################################################
# App Home Page
###############################################################################

@bolt_app.event("app_home_opened")
def amy_home(ack, event, client, say):
    ack()
    global weeklyCompleted
    totalMessages = -1
    stats = statDB.read_item(item = "1", partition_key = "Workspace-wide stats")
    totalMessages = stats.get("total_workspace_messages")
    StatsText = "*Statistics* \nBelow are some statistics from your group channel that you may be interested in!\n Total Messages Sent: %d" %(totalMessages)

    opening = """Welcome to the Amy Bot! I am here to help your team development and psychological saftey.
On this page you can customize certain funcitonalities to best suit your teams needs as well as
check out some interesting statistics from your channel that could help you identify certain things
and allow your team to be more efficient in their work. Also, check out the about tab to see what 
slash commands are available to you!"""
            
    app_home = {
           "type":"home",
           "blocks":[
              {
                 "type":"section",
                 "text":{
                    "type":"mrkdwn",
                    "text": opening
                 }
              },
              {
                #Horizontal divider line 
                "type": "divider"
              },
              {
                  #Section with text and a button
                  "type": "section",
                  "text": {
                    "type": "mrkdwn",
                    "text": "*Brainstorming* \nWould you like to be reminded to revisit your brainstorming session a week after?"
                  },
                  "accessory": {
                        "type": "radio_buttons",
                        "action_id": "Brainstorm_Options",
                
                        "options": [
                        {
                            "value": "1",
                            "text": {
                            "type": "plain_text",
                            "text": "Yes"
                            }   
                        },
                        {
                            "value": "0",
                            "text": {
                            "type": "plain_text",
                            "text": "No"
                            }
                        }]
                    }
                },
                #Horizontal divider line 
                {
                  "type": "divider"
                },
              {
                  #Section with text and a button
                  "type": "section",
                  "text": {
                    "type": "mrkdwn",
                    "text": "*Weekly Survey* \nNumber of People that have completed the survey: %d \nHow often would you like a psychological saftey check in?" %(weeklyCompleted)
                  },
                  "accessory": {
                        "type": "radio_buttons",
                        "action_id": "Weekly_Survey",
                
                        "options": [
                        {
                            "value": "1",
                            "text": {
                            "type": "plain_text",
                            "text": "Once a week"
                            }   
                        },
                        {
                            "value": "2",
                            "text": {
                            "type": "plain_text",
                            "text": "Once every 2 weeks"
                            }
                        },
                        {
                            "value": "3",
                            "text": {
                            "type": "plain_text",
                            "text": "Once every 3 weeks"
                            }
                        }]
                    }
                },
                #Horizontal divider line 
                {
                  "type": "divider"
                },
              {
                  #Section with text and a button
                  "type": "section",
                  "text": {
                    "type": "mrkdwn",
                    "text": StatsText
                  }
                  
                }
           ]
        }

    client.views_publish(
        user_id = event["user"], 
        view = app_home)


@bolt_app.action("Brainstorm_Options")
def brainstorm_options(ack, body, client):
    global brain_weekly
    # Acknowledge the action
    ack()
    form_json = json.dumps(body)
    form_json = form_json[788:]
    actions_index = form_json.find('actions')
    form_json = form_json[actions_index:]
    value_index = form_json.find('value')
    value = form_json[value_index+9]

    if (value == '1'):
        brain_weekly = 1
    else:
        brain_weekly = 0

@bolt_app.action("Weekly_Survey")
def weekly_survey(ack, body, client):
    global weeklySurveyValue
    global weekly_id
    global channel
    # Acknowledge the action
    ack()
    form_json = json.dumps(body)
    form_json = form_json[788:]
    actions_index = form_json.find('actions')
    form_json = form_json[actions_index:]
    value_index = form_json.find('value')
    value = form_json[value_index+9]
    ts = time.time()

    if(weekly_id != ""):
        try:
            client.chat_deleteScheduledMessage(channel = channel, scheduled_message_id = weekly_id)
        except:
            pass

    if (value == '1'):
        weeklySurveyValue = 1
        ts = ts + 604800
        weekly_id = client.chat_scheduleMessage(
            channel = channel,
            text = "Please take your psychological saftey survey using /psych_survey",
            post_at = ts,
        )
    elif (value == '2'):
        weeklySurveyValue = 2
        ts = ts + (2*604800)
        weekly_id = client.chat_scheduleMessage(
            channel = channel,
            text = "Please take your psychological saftey survey using /psych_survey",
            post_at = ts ,
        )
    else:
        weeklySurveyValue = 3
        ts = ts + (3*604800)
        weekly_id = client.chat_scheduleMessage(
            channel = channel,
            text = "Please take your psychological saftey survey using /psych_survey",
            post_at = ts,
        )



###############################################################################

# Once we have our event listeners configured, we can start the
# Flask server with the default `/events` endpoint on port 3000
if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 3000)))
