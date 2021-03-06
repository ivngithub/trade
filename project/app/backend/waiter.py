import hashlib, os
import redis
import requests

from flask_mail import Message
from celery.schedules import crontab

from app import app, celery_app, redis_client
from app import mail
from app.models import db, TypeDealer, NameTask, Task, Category, Position, Characteristic, Image
from . import NLReceiver, logger_app


celery_app.conf.beat_schedule = {
    # Executes every Monday morning at 7:30 a.m.
    "add-every-monday-morning": {
        "task": "app.backend.waiter.test_task",
        "schedule": crontab(minute="*/1")
    },
}
celery_app.conf.timezone = "Europe/Moscow"
celery_app.conf.enable_utc = True


@celery_app.task(bind=True)
def test_task(self):
    # raise Exception("Test exception raised by celery test_task")

    # self.update_state(state="PROGRESS", meta={"current": 0, "total": 0})

    task_name = NameTask.updating_structure_of_catalog.name
    task_id = str(self.request.id)
    # task_state = self.AsyncResult(self.request.id).state

    redis_client.sadd(task_name, task_id)

    import time
    time.sleep(15)
    import random
    r = 1/random.randrange(2)

    redis_client.srem(task_name, task_id)

    return "success"


@celery_app.task(bind=True)
def update_category(self):

    self.update_state(meta={"current": 0, "total": 0})

    task = Task(name=NameTask.updating_structure_of_catalog.value)
    try:

        import time
        time.sleep(15)

        response = NLReceiver(app.config["NL_CATEGORIES"]["URL"].format(catalog_name=app.config["NL_CATALOG_MAIN"]),
                              app.config["NL_CATEGORIES"]["DATA_KEY"])

        nl_error = response.error
        if nl_error:
            raise Exception(str(nl_error))

        root_directory = Category.query.get(1)

        if not root_directory:
            root_directory = Category(name=app.config["PRODUCT_CATALOG_ROOT_DIRECTORY"])
            db.session.add(root_directory)
            db.session.commit()

        categories = sorted(response.data.get("category", []), key=lambda x: int(x["parentId"]))
        categories_in_db = list(map(lambda x: x.nl_id, db.session.query(Category).all()[1:]))

        task_result = {"task_name": task.name, "result": {"new_category": [],
                                                          "old_category": []}}

        for category in categories:
            if int(category["id"]) not in categories_in_db:

                task_result["result"]["new_category"].append(category["name"])

                if int(category["parentId"]) == 0:
                    db.session.add(Category.gen_el_to_db(category, root_directory))
                    db.session.commit()
                else:
                    parent = db.session.query(Category).filter_by(nl_id=int(category["parentId"]),
                                                                  dealer=TypeDealer.nl_dealer.name).first()
                    db.session.add(Category.gen_el_to_db(category, parent))
                    db.session.commit()

        for category_in_db in db.session.query(Category).all()[1:]:

            if category_in_db.nl_id not in list(map(lambda x: int(x["id"]), categories)):
                task_result["result"]["old_category"].append("id: {}, name: {}".format(category_in_db.id,
                                                                                       category_in_db.name))

        task_msg_result = "TASK_NAME: {}, new_category: [{}], old_category [{}]" \
            .format(task_result["task_name"],
                    "; ".join(task_result["result"]["new_category"]),
                    "; ".join(task_result["result"]["old_category"]))

        turn_on_main_categories = db.session.query(Category).filter(Category.nl_parent_id == 0,
                                                                    Category.dealer_operation).all()

        for main_category in turn_on_main_categories:

            for category in main_category.get_subcategories():
                category.dealer_operation = True
                db.session.add(category)
                db.session.commit()

        task_msg_result = "{}, turn_on_main_categories: {}".format(task_msg_result, "success")

        logger_app.info(task_msg_result)

        task.success = True
        task.changes = True if task_result["result"]["new_category"] or \
                               task_result["result"]["old_category"] else False
        task.added = True if task_result["result"]["new_category"] else False
        task.removed = True if task_result["result"]["old_category"] else False
        task.result_msg = task_msg_result

        db.session.add(task)
        db.session.commit()

        subject =  task.name
        body = task_msg_result
        sender = app.config["MAIL_USERNAME"]
        recipients = [app.config["MAIL_ADMIN"], ]
        send_email(subject, body, sender, recipients)

        return "success"

    except Exception as e:
        logger_app.error("{}: {}".format(task.name, str(e)))

        task.success = False
        task.result_msg = str(e)

        db.session.add(task)
        db.session.commit()

        return "failure"


@celery_app.task(bind=True)
def update_position(self):

    task = Task(name=NameTask.updating_positions.value)
    task.success = False
    task.result_msg = "Start update_position"
    db.session.add(task)
    db.session.commit()

    categories_have_positions = db.session.query(Category).filter(Category.turn, Category.nl_leaf).all()
    for category in categories_have_positions:
        response = NLReceiver(app.config["NL_GOODS"]["URL"].format(catalog_name=app.config["NL_CATALOG_MAIN"],
                                                                   category_id=category.nl_id),
                              app.config["NL_GOODS"]["DATA_KEY"])

        for position in Position.gen_el_to_db(response.data):
            try:
                db.session.add(position)
                db.session.commit()
            except Exception as e:
                # logger_app.error("{}: {}".format("Add Position: ", str(e)))
                db.session.rollback()
                position_update = Position.update_position(position)
                try:
                    db.session.add(position_update)
                    db.session.commit()
                except Exception as e:
                    logger_app.error("{}: {}".format("Update Position: ", str(e)))
                    db.session.rollback()
            else:
                #########################
                # This place GET characteristics to position
                response = NLReceiver(app.config["NL_GOOD"]["URL"].format(catalog_name=app.config["NL_CATALOG_MAIN"],
                                                                          category_id=category.nl_id,
                                                                          position_id=position.nl_id),
                                      app.config["NL_GOOD"]["DATA_KEY"])

                characteristics = Characteristic.gen_el_to_db(position, response.data) or {}
                for characteristic in characteristics:
                    try:
                        db.session.add(characteristic)
                        db.session.commit()
                    except Exception as e:
                        db.session.rollback()

                #########################
                # This place GET image to position
                response = NLReceiver(app.config["NL_IMG_GOOD"]["URL"].format(goodsId=position.nl_id),
                                      app.config["NL_IMG_GOOD"]["DATA_KEY"])

                m = hashlib.md5()
                if not response.data:
                    with open(app.config["DEFAULT_IMAGE_FOR_CATALOG"], "rb") as response_image:
                        response_image_content = response_image.read()
                        m.update(response_image_content)
                else:
                    url_image = response.data["items"][0]["properties"]["Url"]
                    response_image = requests.get(
                        url_image.rsplit("&", 1)[0] + app.config["LOGOTYPE"])  # it is string fot URL
                    response_image_content = response_image.content
                    m.update(response_image_content)

                hash_image = m.hexdigest()
                image = db.session.query(Image).filter(Image.hash==hash_image).first()
                if not image:
                    sub_folder_name = hash_image[0:2]
                    sub_folder = os.path.join(app.config["UPLOAD_FOLDER"], sub_folder_name)
                    if not os.path.exists(sub_folder):
                        os.mkdir(sub_folder)

                    image_name = hash_image + ".jpg"
                    path_to_image = os.path.join(sub_folder, image_name)
                    with open(path_to_image, "wb") as f:
                        f.write(response_image_content)

                    image = Image(original_name=image_name, name=image_name, hash=hash_image,
                                  path=os.path.join(sub_folder_name, image_name))

                position.images.append(image)
                db.session.add(position)
                db.session.commit()

    task.success = True
    task.result_msg = "Finish update_position"
    db.session.add(task)
    db.session.commit()

@celery_app.task
def gen_prime(x):
    multiples = []
    results = []
    for i in range(2, x+1):
        if i not in multiples:
            results.append(i)
            for j in range(i*i, x+1, i):
                multiples.append(j)

    return results


@celery_app.task
def send_email(subject, body, sender, recipients):

    task = Task(name=NameTask.sending_email.value)

    msg = Message(subject, sender=sender, recipients=recipients)
    msg.body = body

    # msg.html = html_body
    # if attachments:
    #     for attachment in attachments:
    #         msg.attach(*attachment)
    try:
        mail.send(msg)

        return "success"

    except Exception as e:

        logger_app.error("{}: {}".format(task.name, e))

        return "failure"


if __name__ == "__main__":

    pass
