FROM public.ecr.aws/lambda/python:3.12

COPY requirements.txt ${LAMBDA_TASK_ROOT}/requirements.txt
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

COPY src/codes ${LAMBDA_TASK_ROOT}/src/codes
COPY src/input ${LAMBDA_TASK_ROOT}/src/input

CMD ["src.codes.lambda_handler.lambda_handler"]
